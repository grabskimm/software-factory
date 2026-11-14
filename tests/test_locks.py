"""Repo lock backends: flock, Azure Blob lease with fencing, managed identity tokens.

The blob tests run against tests/fake_blob.py on 127.0.0.1 with a fake clock, so lease
expiry is simulated by advancing time. Every token below is an obviously fake value.
"""

import http.server
import json
import logging
import os
import re
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from factory import locks
from factory.locks import (
    BlobLeaseLock,
    LocalFlockLock,
    LockBusy,
    LockConfigError,
    LockError,
    LockLost,
    LockUnavailable,
    ManagedIdentityToken,
    RepoLock,
    TokenError,
    WorkloadIdentityToken,
    lock_from_env,
    register_kill_hook,
    token_source_from_env,
    unregister_kill_hook,
    validate_authority_host,
)
from tests.fake_blob import FakeBlobServer, FakeClock

locks.log.addHandler(logging.NullHandler())  # keep expected loss messages out of test output

TOKEN = "fake-token-AAAA.BBBB.CCCC"
BUSY = "another factory supervisor holds the repository lock"
CONTAINER, BLOB = "locks", "acme__widgets"


def raw(url, method, headers=None):
    """A request made outside BlobLeaseLock, as a stale or hostile client would."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, error.headers


class Ticker:
    """Injectable `sleep` that parks the renewal thread until the test lets it run.

    Each release advances the fake clock by the requested sleep, then the thread does
    exactly one renewal. This makes the thread tests deterministic and instant.
    """

    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self._cv = threading.Condition()
        self._parked = self._go = self._closed = False

    def sleep(self, seconds):
        with self._cv:
            self.calls.append(seconds)
            self._parked = True
            self._cv.notify_all()
            self._cv.wait_for(lambda: self._go or self._closed, timeout=30)
            go, self._go, self._parked = self._go, False, False
            if not go:
                return
        self.clock.advance(seconds)

    def release_tick(self):
        """Let the parked thread run one tick without waiting for it to finish."""
        with self._cv:
            assert self._cv.wait_for(lambda: self._parked, timeout=10), "thread not parked"
            self._go = True
            self._cv.notify_all()

    def step(self):
        """Run one tick and wait until the thread is parked again (tick finished)."""
        self.release_tick()
        with self._cv:
            assert self._cv.wait_for(lambda: self._parked and not self._go, timeout=10)

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class BlobTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.server = FakeBlobServer(clock=self.clock, tokens={TOKEN}).start()
        self.addCleanup(self.server.stop)
        self.token_calls = 0
        self.lost_calls = []
        self.lost = threading.Event()

    def provider(self):
        self.token_calls += 1
        return TOKEN

    def on_lost(self):
        self.lost_calls.append(1)
        self.lost.set()

    def make(self, **overrides):
        ticker = Ticker(self.clock)
        options = {
            "account_url": self.server.url,
            "container": CONTAINER,
            "blob_name": BLOB,
            "token_provider": self.provider,
            "on_lost": self.on_lost,
            "clock": self.clock,
            "sleep": ticker.sleep,
        }
        options.update(overrides)
        lock = BlobLeaseLock(**options)
        lock.ticker = ticker
        self.addCleanup(ticker.close)
        self.addCleanup(lock.release)
        return lock

    def blob(self):
        return self.server.blob(CONTAINER, BLOB)

    def n(self, op=None):
        return len(self.server.calls(op))


class LocalFlockLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "factory.lock"

    def test_two_instances_exclude_each_other_until_release(self):
        first, second = LocalFlockLock(self.path), LocalFlockLock(self.path)
        first.acquire()
        with self.assertRaisesRegex(LockBusy, BUSY) as caught:
            second.acquire()
        self.assertIsInstance(caught.exception, RuntimeError)
        with self.assertRaises(LockLost):  # a failed acquire holds nothing
            second.assert_held()
        first.release()
        second.acquire()
        second.assert_held()
        second.release()

    def test_assert_held_tracks_the_lifecycle_and_token_is_zero(self):
        lock = LocalFlockLock(self.path)
        with self.assertRaisesRegex(LockLost, "not held"):
            lock.assert_held()
        with self.assertRaises(LockLost):
            lock.renew()
        self.assertIs(lock.acquire(), lock)
        self.assertIs(lock.acquire(), lock)  # idempotent
        lock.assert_held()
        lock.renew()
        self.assertEqual(lock.fencing_token, 0)
        lock.release()
        lock.release()  # idempotent, never raises
        with self.assertRaises(LockLost):
            lock.assert_held()

    def test_context_manager_releases_on_exception(self):
        with self.assertRaisesRegex(ValueError, "boom"):
            with LocalFlockLock(self.path) as lock:
                self.assertIsInstance(lock, RepoLock)
                lock.assert_held()
                raise ValueError("boom")
        with LocalFlockLock(self.path):  # would raise LockBusy if the first never released
            pass

    def test_failed_enter_does_not_release_the_other_holder(self):
        with LocalFlockLock(self.path):
            with self.assertRaises(LockBusy):
                with LocalFlockLock(self.path):
                    self.fail("entered a held lock")
            with self.assertRaises(LockBusy):  # still held by the outer block
                LocalFlockLock(self.path).acquire()

    def test_removed_lock_file_is_not_a_hold(self):
        lock = LocalFlockLock(self.path).acquire()
        self.addCleanup(lock.release)
        self.path.unlink()
        with self.assertRaisesRegex(LockLost, "removed or replaced"):
            lock.assert_held()
        # A second supervisor can lock the recreated file, which is why we must not trust ours.
        other = LocalFlockLock(self.path).acquire()
        other.release()

    def test_matches_the_state_lock_message(self):
        from factory.state import lock as state_lock

        with state_lock(self.path):
            with self.assertRaisesRegex(RuntimeError, BUSY):
                LocalFlockLock(self.path).acquire()


class BlobAcquireTests(BlobTestCase):
    def test_acquire_busy_release_and_request_shape(self):
        holder, other = self.make(), self.make()
        self.assertIs(holder.acquire(), holder)
        self.assertEqual(holder.fencing_token, 1)
        self.assertEqual(self.server.ops(), ["put_blob", "acquire", "properties", "metadata"])
        with self.assertRaisesRegex(LockBusy, BUSY):
            other.acquire()
        self.assertEqual(self.server.ops()[4:], ["put_blob", "acquire"])  # busy touches no fence
        self.assertEqual(self.blob().metadata, {"fence": "1"})

        create, acquire, _, metadata = self.server.calls()[:4]
        self.assertEqual(create.path, f"/{CONTAINER}/{BLOB}")
        self.assertEqual(create.headers["if-none-match"], "*")
        self.assertEqual(create.headers["x-ms-blob-type"], "BlockBlob")
        for record in self.server.calls():
            self.assertEqual(record.headers["authorization"], f"Bearer {TOKEN}")
            self.assertEqual(record.headers["x-ms-version"], "2023-11-03")
        self.assertEqual(acquire.headers["x-ms-lease-duration"], "60")
        lease_id = acquire.headers["x-ms-proposed-lease-id"]
        self.assertEqual(str(uuid.UUID(lease_id)), lease_id)
        self.assertEqual(metadata.lease_id, lease_id)
        self.assertEqual(metadata.headers["x-ms-meta-fence"], "1")
        # The lock never overwrites the blob: that would reset the fencing counter.
        for record in self.server.calls("put_blob"):
            self.assertEqual(record.headers.get("if-none-match"), "*")
        self.assertEqual(self.token_calls, len(self.server.calls()))

        holder.release()
        self.assertEqual(self.blob().state, "available")
        other.acquire()
        self.assertEqual(other.fencing_token, 2)

    def test_existing_blob_is_reused_whichever_status_the_service_uses(self):
        for status in (409, 412):
            with self.subTest(status=status):
                server = FakeBlobServer(clock=self.clock, tokens={TOKEN}, exists_status=status)
                server.start()
                self.addCleanup(server.stop)
                lock = self.make(account_url=server.url)
                lock.acquire()
                lock.release()
                again = self.make(account_url=server.url)
                again.acquire()
                self.assertEqual(again.fencing_token, 2)
                self.assertEqual([r.status for r in server.calls("put_blob")], [201, status])

    def test_acquire_twice_is_a_no_op_and_reacquire_after_release_bumps_fence(self):
        lock = self.make()
        lock.acquire()
        before = self.n()
        lock.acquire()
        self.assertEqual(self.n(), before)
        lock.release()
        lock.acquire()
        self.assertEqual(lock.fencing_token, 2)
        lock.assert_held()

    def test_blob_deleted_between_create_and_lease_is_recreated_once(self):
        lock = self.make()
        original = self.server._dispatch

        def delete_once(method, path, op, headers):
            if op == "acquire" and not getattr(delete_once, "done", False):
                delete_once.done = True
                self.server.delete(CONTAINER, BLOB)
            return original(method, path, op, headers)

        with patch.object(self.server, "_dispatch", delete_once):
            lock.acquire()
        self.assertEqual(lock.fencing_token, 1)
        self.assertEqual(self.server.ops().count("put_blob"), 2)

    def test_unusable_service_is_unavailable_not_busy(self):
        for status, expected in ((403, "403"), (500, "500"), (404, "404")):
            with self.subTest(status=status):
                self.server.inject("any", status=status)
                lock = self.make()
                with self.assertRaisesRegex(LockUnavailable, expected):
                    lock.acquire()
                with self.assertRaises(LockLost):  # nothing is held after a failed acquire
                    lock.assert_held()

    def test_unknown_container_is_unavailable(self):
        lock = self.make(container="nope-container")
        with self.assertRaisesRegex(LockUnavailable, "ContainerNotFound"):
            lock.acquire()

    def test_failure_after_lease_acquired_releases_it(self):
        # Metadata write refused with 412: we lost the lease immediately.
        self.server.inject("metadata", status=412)
        lock = self.make()
        with self.assertRaises(LockLost):
            lock.acquire()
        self.assertEqual(self.blob().state, "available")  # released, next replica need not wait
        self.assertEqual(self.server.ops()[-1], "release")
        # A 503 on the metadata write is infrastructure trouble, still releases.
        self.server.inject("metadata", status=503)
        lock = self.make()
        with self.assertRaisesRegex(LockUnavailable, "503"):
            lock.acquire()
        self.assertEqual(self.blob().state, "available")
        self.assertEqual(self.blob().metadata.get("fence"), None)
        with self.assertRaises(LockLost):
            lock.assert_held()
        self.assertEqual(self.make().acquire().fencing_token, 1)

    def test_corrupt_fence_metadata_fails_closed(self):
        self.make().acquire().release()
        for bad in ("abc", "-1", "", "1" * 40, "3.5"):
            with self.subTest(bad=bad):
                self.blob().metadata["fence"] = bad
                lock = self.make()
                with self.assertRaisesRegex(LockUnavailable, "corrupt"):
                    lock.acquire()
                self.assertEqual(self.blob().state, "available")  # lease not left dangling

    def test_context_manager_acquires_and_releases_even_on_error(self):
        with self.assertRaisesRegex(ValueError, "boom"):
            with self.make() as lock:
                lock.assert_held()
                raise ValueError("boom")
        self.assertEqual(self.blob().state, "available")
        holder = self.make().acquire()
        sent = self.n("release")
        with self.assertRaises(LockBusy):
            with self.make():
                self.fail("entered a held lock")
        self.assertEqual(self.n("release"), sent)  # failed enter never releases the holder
        holder.assert_held()


class BlobFencingTests(BlobTestCase):
    def test_fencing_counter_increments_across_holders(self):
        seen = []
        for _ in range(3):
            with self.make() as lock:
                seen.append(lock.fencing_token)
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(self.blob().metadata, {"fence": "3"})

    def test_counter_survives_a_holder_that_never_released(self):
        crashed = self.make().acquire()
        self.clock.advance(61)  # its lease lapses; nobody renewed
        successor = self.make().acquire()
        self.assertEqual(successor.fencing_token, 2)
        self.assertGreater(successor.fencing_token, crashed.fencing_token)
        self.assertEqual(self.blob().metadata, {"fence": "2"})

    def test_stale_holders_metadata_write_is_rejected_with_412(self):
        stale = self.make().acquire()
        stale_id = self.server.calls("metadata")[0].lease_id
        self.server.break_lease(CONTAINER, BLOB)
        current = self.make().acquire()
        self.assertEqual(current.fencing_token, 2)
        current_id = self.server.calls("metadata")[-1].lease_id
        self.assertNotEqual(current_id, stale_id)
        url = f"{self.server.url}/{CONTAINER}/{BLOB}?comp=metadata"
        base = {"Authorization": f"Bearer {TOKEN}", "x-ms-version": "2023-11-03"}

        def write(lease_id):
            headers = dict(base, **{"x-ms-meta-fence": "99"})
            if lease_id:
                headers["x-ms-lease-id"] = lease_id
            status, reply = raw(url, "PUT", headers)
            return status, reply.get("x-ms-error-code")

        self.assertEqual(write(stale_id), (412, "LeaseIdMismatchWithBlobOperation"))
        self.assertEqual(write(None), (412, "LeaseIdMissing"))
        self.assertEqual(self.blob().metadata, {"fence": "2"})  # neither write landed
        # The lease lapses without a new holder: an old id is still refused.
        self.clock.advance(61)
        self.assertEqual(write(current_id), (412, "LeaseLost"))
        current.release()
        self.assertEqual(write(stale_id), (412, "LeaseNotPresentWithBlobOperation"))
        with self.assertRaises(LockLost):
            stale.renew()

    def test_the_stale_holder_cannot_act_once_superseded(self):
        stale = self.make().acquire()
        self.clock.advance(61)
        self.make().acquire()
        with self.assertRaises(LockLost):
            stale.assert_held()  # local age alone proves it
        self.clock.advance(-61 + 10)  # even with a confused local clock the server says no
        with self.assertRaises(LockLost):
            stale.assert_held()
        self.assertEqual(self.lost_calls, [1])


class BlobRenewalTests(BlobTestCase):
    def test_renewal_thread_keeps_the_lease_past_its_duration(self):
        lock = self.make().acquire()
        rival = self.make()
        for _ in range(6):  # 6 x 15 s = 90 s of fake time, more than one 60 s lease
            lock.ticker.step()
        self.assertEqual(self.n("renew"), 6)
        self.assertEqual(set(lock.ticker.calls), {15})
        for record in self.server.calls("renew"):
            self.assertEqual(record.lease_id, self.server.calls("metadata")[0].lease_id)
        with self.assertRaises(LockBusy):
            rival.acquire()
        lock.assert_held()
        self.assertEqual(self.lost_calls, [])

    def test_without_renewal_the_lease_expires_and_assert_held_refuses(self):
        lock = self.make().acquire()
        self.clock.advance(51)  # lease_seconds 60 minus the 10 s safety margin has passed
        sent = self.n()
        with self.assertRaisesRegex(LockLost, "may have expired"):
            lock.assert_held()
        self.assertEqual(self.n(), sent)  # refused locally; no round trip can prove it
        self.assertEqual(self.lost_calls, [])  # an assertion is not a loss event
        self.clock.advance(10)
        rival = self.make().acquire()  # the server agrees the lease lapsed
        self.assertEqual(rival.fencing_token, 2)

    def test_assert_held_is_free_when_fresh_and_renews_once_when_older_than_5s(self):
        lock = self.make().acquire()
        sent = self.n()
        self.clock.advance(4.9)
        lock.assert_held()
        self.assertEqual(self.n(), sent)
        self.clock.advance(1.2)  # 6.1 s since the last proof
        lock.assert_held()
        self.assertEqual(self.n(), sent + 1)
        self.assertEqual(self.server.ops()[-1], "renew")
        lock.assert_held()  # the renewal made it fresh again
        self.assertEqual(self.n(), sent + 1)

    def test_sync_renew_extends_the_lease_so_assert_held_keeps_passing(self):
        lock = self.make().acquire()
        for _ in range(8):  # every 40 s, never reaching the 50 s refusal threshold
            self.clock.advance(40)
            lock.assert_held()
        self.assertEqual(self.n("renew"), 8)
        with self.assertRaises(LockBusy):
            self.make().acquire()

    def test_failed_sync_renew_raises_without_marking_lost(self):
        lock = self.make().acquire()
        self.clock.advance(6)
        self.server.inject("renew", status=None)  # connection dropped
        with self.assertRaisesRegex(LockLost, "cannot prove"):
            lock.assert_held()
        self.assertEqual(self.lost_calls, [])
        lock.assert_held()  # the next attempt works; one failure is within budget
        self.assertEqual(self.lost_calls, [])

    def test_sync_failures_count_toward_the_consecutive_budget(self):
        lock = self.make().acquire()
        self.server.inject("renew", times=2, status=503)
        self.clock.advance(6)
        with self.assertRaises(LockLost):
            lock.assert_held()
        self.assertEqual(self.lost_calls, [])
        with self.assertRaises(LockLost):
            lock.assert_held()
        self.assertEqual(self.lost_calls, [1])
        self.server.requests.clear()
        with self.assertRaisesRegex(LockLost, "consecutive"):
            lock.assert_held()  # lost for good: no further round trips
        self.assertEqual(self.n(), 0)

    def test_sync_renew_that_finds_the_lease_taken_is_a_definite_loss(self):
        lock = self.make().acquire()
        self.server.break_lease(CONTAINER, BLOB)
        self.make().acquire()
        self.clock.advance(6)
        with self.assertRaises(LockLost):
            lock.assert_held()
        self.assertEqual(self.lost_calls, [1])

    def test_concurrent_assert_held_does_one_round_trip(self):
        lock = self.make().acquire()
        self.clock.advance(6)
        errors = []

        def check():
            try:
                lock.assert_held()
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=check) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.n("renew"), 1)


class BlobLossTests(BlobTestCase):
    def test_broken_lease_taken_by_another_client_is_lost(self):
        lock = self.make().acquire()
        self.server.break_lease(CONTAINER, BLOB)
        rival = self.make().acquire()
        with self.assertRaisesRegex(LockLost, "409"):
            lock.renew()
        self.assertEqual(self.lost_calls, [1])
        with self.assertRaises(LockLost):
            lock.assert_held()
        with self.assertRaises(LockLost):
            lock.renew()
        self.assertEqual(self.lost_calls, [1])  # on_lost fires once per loss
        sent = self.n()
        lock.release()  # must not release the rival's lease
        self.assertEqual(self.n(), sent)
        rival.assert_held()
        self.assertEqual(self.blob().state, "leased")

    def test_renewal_thread_reports_a_definite_loss_immediately(self):
        lock = self.make().acquire()
        self.server.break_lease(CONTAINER, BLOB)
        self.make().acquire()
        lock.ticker.release_tick()
        self.assertTrue(self.lost.wait(5))
        self.assertEqual(self.n("renew"), 1)  # no retries after a definite loss
        lock._thread.join(5)
        self.assertFalse(lock._thread.is_alive())
        with self.assertRaises(LockLost):
            lock.assert_held()

    def test_every_definite_loss_status_marks_the_lock_lost(self):
        for status in (409, 412, 404):
            with self.subTest(status=status):
                self.lost_calls.clear()
                lock = self.make().acquire()
                self.server.inject("renew", status=status)
                with self.assertRaises(LockLost):
                    lock.renew()
                self.assertEqual(self.lost_calls, [1])
                lock.release()
                self.server.break_lease(CONTAINER, BLOB)  # let the next subtest acquire

    def test_deleted_lock_blob_is_a_loss(self):
        lock = self.make().acquire()
        self.server.delete(CONTAINER, BLOB)
        with self.assertRaises(LockLost):
            lock.renew()
        self.assertEqual(self.lost_calls, [1])

    def test_two_consecutive_network_failures_call_on_lost(self):
        lock = self.make().acquire()
        self.server.inject("renew", times=2, status=None)
        with self.assertRaisesRegex(LockUnavailable, "1 of 2"):
            lock.renew()
        self.assertEqual(self.lost_calls, [])
        lock.assert_held()  # one failure so far: still provable (fresh enough)
        with self.assertRaisesRegex(LockLost, "2 consecutive"):
            lock.renew()
        self.assertEqual(self.lost_calls, [1])

    def test_two_consecutive_failures_in_the_thread_call_on_lost(self):
        lock = self.make().acquire()
        self.server.inject("renew", times=2, status=None)
        lock.ticker.step()  # failure 1: thread survives and parks again
        self.assertEqual(self.lost_calls, [])
        lock.ticker.release_tick()  # failure 2
        self.assertTrue(self.lost.wait(5))
        self.assertEqual(self.lost_calls, [1])
        lock._thread.join(5)
        self.assertFalse(lock._thread.is_alive())

    def test_server_errors_and_auth_failures_count_as_failed_renewals(self):
        for status in (500, 503, 429, 403, 401):
            with self.subTest(status=status):
                self.lost_calls.clear()
                lock = self.make().acquire()
                self.server.inject("renew", times=2, status=status)
                with self.assertRaises(LockUnavailable):
                    lock.renew()
                with self.assertRaises(LockLost):
                    lock.renew()
                self.assertEqual(self.lost_calls, [1])
                lock.release()
                self.server.break_lease(CONTAINER, BLOB)

    def test_one_failure_then_success_is_not_lost_and_resets_the_count(self):
        lock = self.make().acquire()
        for _ in range(3):  # fail, succeed, fail, succeed...: never two in a row
            self.server.inject("renew", status=500)
            with self.assertRaises(LockUnavailable):
                lock.renew()
            lock.renew()
        self.assertEqual(self.lost_calls, [])
        lock.assert_held()
        self.assertEqual(lock._failures, 0)

    def test_one_failure_then_success_in_the_thread(self):
        lock = self.make().acquire()
        self.server.inject("renew", status=None)
        lock.ticker.step()
        lock.ticker.step()
        self.assertEqual(self.n("renew"), 2)
        self.assertEqual(self.lost_calls, [])
        lock.assert_held()

    def test_failure_budget_is_configurable(self):
        lock = self.make(max_failed_renewals=3).acquire()
        self.server.inject("renew", times=3, status=503)
        for _ in range(2):
            with self.assertRaises(LockUnavailable):
                lock.renew()
        self.assertEqual(self.lost_calls, [])
        with self.assertRaises(LockLost):
            lock.renew()
        self.assertEqual(self.lost_calls, [1])

    def test_partition_then_heal_within_budget(self):
        lock = self.make().acquire()
        self.server.partitioned = True
        with self.assertRaises(LockUnavailable):
            lock.renew()
        self.server.partitioned = False
        lock.renew()
        self.assertEqual(self.lost_calls, [])

    def test_a_crashing_renewal_thread_fails_closed(self):
        lock = self.make().acquire()
        with patch.object(lock, "_renew_round", side_effect=KeyError("bug")):
            lock.ticker.release_tick()
            self.assertTrue(self.lost.wait(5))
        lock._thread.join(5)
        self.assertFalse(lock._thread.is_alive())
        with self.assertRaisesRegex(LockLost, "renewal thread failed"):
            lock.assert_held()

    def test_any_unexpected_error_while_proving_the_hold_is_a_loss_of_proof(self):
        lock = self.make().acquire()
        self.clock.advance(6)
        with patch.object(lock, "_renew_request", side_effect=OSError("odd")):
            with self.assertRaisesRegex(LockLost, "cannot prove"):
                lock.assert_held()
        with patch.object(lock, "_renew_round", side_effect=ZeroDivisionError):
            with self.assertRaisesRegex(LockLost, "ZeroDivisionError"):
                lock.assert_held()

    def test_a_failing_on_lost_callback_does_not_hide_the_loss(self):
        def explode():
            raise RuntimeError("callback bug")

        lock = self.make(on_lost=explode).acquire()
        self.server.break_lease(CONTAINER, BLOB)
        self.make().acquire()
        with self.assertRaises(LockLost):
            lock.renew()

    def test_default_on_lost_runs_registered_hooks_then_exits_75(self):
        order = []
        hook = lambda: order.append("kill-children")  # noqa: E731

        def broken():
            order.append("broken")
            raise RuntimeError("hook bug")

        register_kill_hook(hook)
        register_kill_hook(hook)  # registering twice must not run it twice
        register_kill_hook(broken)
        self.addCleanup(unregister_kill_hook, hook)
        self.addCleanup(unregister_kill_hook, broken)
        lock = self.make(on_lost=None).acquire()  # None selects the default behaviour
        self.server.break_lease(CONTAINER, BLOB)
        self.make().acquire()
        with patch("os._exit") as fake_exit:
            with self.assertRaises(LockLost):
                lock.renew()
        fake_exit.assert_called_once_with(75)
        self.assertEqual(sorted(order), ["broken", "kill-children"])

    def test_a_hung_kill_hook_cannot_delay_the_exit_forever(self):
        release = threading.Event()
        self.addCleanup(release.set)
        ran = threading.Event()
        hang = lambda: release.wait(30)  # noqa: E731
        fast = ran.set
        register_kill_hook(hang)
        register_kill_hook(fast)
        self.addCleanup(unregister_kill_hook, hang)
        self.addCleanup(unregister_kill_hook, fast)
        with patch.object(locks, "KILL_HOOK_TIMEOUT", 0.2), patch("os._exit") as fake_exit:
            locks.exit_process_on_lost()
        fake_exit.assert_called_once_with(75)
        self.assertTrue(ran.is_set())  # the hung hook did not stop the others

    def test_exit_happens_even_if_the_hooks_cannot_run(self):
        with patch.object(locks, "run_kill_hooks", side_effect=RuntimeError("no threads")):
            with patch("os._exit") as fake_exit:
                with self.assertRaises(RuntimeError):
                    locks.exit_process_on_lost()
        fake_exit.assert_called_once_with(75)

    def test_default_on_lost_not_used_when_a_callback_is_given(self):
        lock = self.make().acquire()
        self.server.break_lease(CONTAINER, BLOB)
        self.make().acquire()
        with patch("os._exit") as fake_exit:
            with self.assertRaises(LockLost):
                lock.renew()
        fake_exit.assert_not_called()


class BlobReleaseTests(BlobTestCase):
    def test_release_is_idempotent_and_stops_the_renewal_thread(self):
        lock = self.make().acquire()
        lock.release()
        lock.release()
        self.assertEqual(self.n("release"), 1)
        with self.assertRaisesRegex(LockLost, "released"):
            lock.assert_held()
        lock.ticker.release_tick()  # the parked thread wakes and must just exit
        lock._thread.join(5)
        self.assertFalse(lock._thread.is_alive())
        self.assertEqual(self.n("renew"), 0)
        self.assertEqual(self.lost_calls, [])  # releasing is never a loss

    def test_release_never_raises_when_the_service_is_gone(self):
        lock = self.make().acquire()
        self.server.stop()
        lock.release()  # connection refused
        broken = self.make(token_provider=lambda: 1 / 0)
        broken._state = "held"  # as if acquired earlier
        broken._lease_id = str(uuid.uuid4())
        broken.release()  # token provider blew up
        self.assertEqual(self.lost_calls, [])

    def test_release_of_a_lock_never_acquired_does_nothing(self):
        lock = self.make()
        lock.release()
        self.assertEqual(self.n(), 0)

    def test_default_sleep_wakes_promptly_on_release(self):
        lock = self.make(sleep=None).acquire()
        lock.release()
        lock._thread.join(5)
        self.assertFalse(lock._thread.is_alive())

    def test_a_renewal_in_flight_during_release_is_not_a_loss(self):
        lock = self.make().acquire()
        entered, proceed = threading.Event(), threading.Event()
        original = lock._renew_request

        def slow(lease_id):
            entered.set()
            proceed.wait(5)
            return original(lease_id)

        with patch.object(lock, "_renew_request", slow):
            lock.ticker.release_tick()
            self.assertTrue(entered.wait(5))
            releaser = threading.Thread(target=lock.release)
            releaser.start()
            for _ in range(500):  # release has marked the lock released and waits for the renewal
                if lock._state == "released":
                    break
                threading.Event().wait(0.01)
            self.assertEqual(lock._state, "released")
            proceed.set()
            releaser.join(10)
        lock._thread.join(5)
        self.assertEqual(self.lost_calls, [])
        self.assertEqual(self.blob().state, "available")


class BlobSecretsTests(BlobTestCase):
    def assertClean(self, error):
        """No token anywhere in the exception, its printed traceback, or its hidden chain."""
        texts = ["".join(traceback.format_exception(error))]
        while error is not None:
            texts.append(f"{error!r} {error}")
            error = error.__cause__ or error.__context__
        for text in texts:
            self.assertNotIn("fake-token", text)

    def test_provider_is_called_per_request_and_token_never_leaks_from_failures(self):
        # Wrong token: the service answers 401 and the error carries status only.
        server = FakeBlobServer(clock=self.clock, tokens={"some-other-token"}).start()
        self.addCleanup(server.stop)
        lock = self.make(account_url=server.url)
        with self.assertLogs("factory.locks", level="WARNING") as logs:
            with self.assertRaisesRegex(LockUnavailable, "401") as caught:
                lock.acquire()
            self.assertClean(caught.exception)
            self.assertEqual(self.token_calls, 1)
            locks.log.warning("sentinel")  # assertLogs needs at least one record
        self.assertNotIn("fake-token", "\n".join(logs.output))

    def test_provider_exception_text_is_not_propagated(self):
        def leaky():
            raise RuntimeError(f"cannot fetch {TOKEN}")

        lock = self.make(token_provider=leaky)
        with self.assertRaises(LockUnavailable) as caught:
            lock.acquire()
        self.assertIn("RuntimeError", str(caught.exception))  # the class name only
        self.assertClean(caught.exception)

    def test_unusable_tokens_are_refused_before_they_reach_a_header(self):
        for bad in ("a\r\nX-Evil: 1", "has space", "", None, 123, "x" * 9000):
            with self.subTest(bad=repr(bad)[:20]):
                lock = self.make(token_provider=lambda bad=bad: bad)
                with self.assertRaisesRegex(LockUnavailable, "unusable token"):
                    lock.acquire()
        self.assertEqual(self.n(), 0)

    def test_failures_during_renewal_do_not_leak_the_token(self):
        lock = self.make().acquire()
        self.server.stop()
        for _ in range(2):
            with self.assertRaises(LockError) as caught:
                lock.renew()
            self.assertClean(caught.exception)

    def test_lock_repr_and_url_hold_no_credentials(self):
        lock = self.make()
        self.assertNotIn(TOKEN, repr(lock))
        self.assertNotIn("@", lock._blob_url)

    def test_redirects_are_not_followed_with_the_bearer_token(self):
        target = self.server

        class Redirector(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_PUT(self):
                self.send_response(307)
                self.send_header("Location", target.url + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_HEAD = do_PUT

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        lock = self.make(account_url=f"http://127.0.0.1:{httpd.server_address[1]}")
        with self.assertRaisesRegex(LockUnavailable, "307"):
            lock.acquire()
        self.assertEqual(self.server.requests, [])  # the token never went to the target

    def test_proxy_environment_is_ignored(self):
        env = {name: "http://127.0.0.1:9" for name in ("http_proxy", "HTTP_PROXY")}
        env.update(no_proxy="", NO_PROXY="")
        with patch.dict(os.environ, env):
            self.make().acquire()  # would fail if the dead proxy were used


class BlobConstructionTests(unittest.TestCase):
    def build(self, **overrides):
        options = dict(
            account_url="https://acct.blob.core.windows.net",
            container="locks",
            blob_name="acme__widgets",
            token_provider=lambda: TOKEN,
        )
        options.update(overrides)
        return BlobLeaseLock(**options)

    def test_valid_and_invalid_arguments(self):
        lock = self.build()
        self.assertEqual(
            lock._blob_url, "https://acct.blob.core.windows.net/locks/acme__widgets"
        )
        self.assertEqual(self.build(account_url="http://127.0.0.1:1")._blob_url[:17], "http://127.0.0.1:")
        for override in (
            {"account_url": "http://acct.blob.core.windows.net"},  # token over cleartext
            {"account_url": "file:///etc"},
            {"account_url": "https://user:pw@acct.blob.core.windows.net"},
            {"account_url": "https://acct.blob.core.windows.net/?sig=abc"},
            {"account_url": "https://acct.blob.core.windows.net/#frag"},
            {"account_url": "https:///nohost"},
            {"account_url": "https://acct.blob.core.windows.net:notaport"},
            {"container": "Locks"},
            {"container": "a"},
            {"container": "locks/../x"},
            {"blob_name": "../x"},
            {"blob_name": ".."},
            {"blob_name": "org/repo"},
            {"blob_name": "a?b"},
            {"blob_name": ""},
            {"blob_name": "ok\n"},
            {"lease_seconds": 14},
            {"lease_seconds": 61},
            {"lease_seconds": -1},
            {"renew_every": 0},
            {"renew_every": 45},  # no room for the 10 s margin plus a request
            {"max_failed_renewals": 0},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.build(**override)

    def test_blob_names_with_dots_and_dashes_are_quoted_safely(self):
        lock = self.build(blob_name="my-org__repo.name_x")
        self.assertTrue(lock._blob_url.endswith("/locks/my-org__repo.name_x"))


class FactoryFromEnvTests(unittest.TestCase):
    def test_local_lock_without_account_url(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "factory.lock"
            for env in ({}, {"FACTORY_LOCK_ACCOUNT_URL": "  "}):
                lock = lock_from_env("acme__widgets", path, env)
                self.assertIsInstance(lock, LocalFlockLock)
                self.assertEqual(lock.path, path)

    def test_blob_lock_from_env(self):
        env = {"FACTORY_LOCK_ACCOUNT_URL": "https://acct.blob.core.windows.net"}
        lock = lock_from_env("acme__widgets", "/unused", env)
        self.assertIsInstance(lock, BlobLeaseLock)
        self.assertEqual(lock._blob_url, "https://acct.blob.core.windows.net/locks/acme__widgets")
        self.assertIsInstance(lock._token_provider, ManagedIdentityToken)
        self.assertIsNone(lock._token_provider._client_id)
        env.update(FACTORY_LOCK_CONTAINER="custom-locks", AZURE_CLIENT_ID="client-a")
        lock = lock_from_env("acme__widgets", "/unused", env)
        self.assertTrue(lock._blob_url.endswith("/custom-locks/acme__widgets"))
        self.assertEqual(lock._token_provider._client_id, "client-a")
        env.update(FACTORY_LOCK_CLIENT_ID="client-b")
        self.assertEqual(
            lock_from_env("acme__widgets", "/unused", env)._token_provider._client_id, "client-b"
        )

    def test_blob_options_pass_through_and_bad_input_is_rejected(self):
        env = {"FACTORY_LOCK_ACCOUNT_URL": "https://acct.blob.core.windows.net"}
        provider = lambda: TOKEN  # noqa: E731
        lock = lock_from_env(
            "acme__widgets", "/unused", env, token_provider=provider, lease_seconds=45
        )
        self.assertIs(lock._token_provider, provider)
        self.assertEqual(lock._lease_seconds, 45)
        with self.assertRaises(ValueError):
            lock_from_env("acme/widgets", "/unused", env)
        with self.assertRaises(ValueError):
            lock_from_env("acme__widgets", "/unused", {"FACTORY_LOCK_ACCOUNT_URL": "http://x.example"})

    def test_reads_the_process_environment_by_default(self):
        with patch.dict(os.environ, {"FACTORY_LOCK_ACCOUNT_URL": "https://acct.blob.core.windows.net"}):
            self.assertIsInstance(lock_from_env("acme__widgets", "/unused"), BlobLeaseLock)
        with patch.dict(os.environ):
            os.environ.pop("FACTORY_LOCK_ACCOUNT_URL", None)
            self.assertIsInstance(lock_from_env("acme__widgets", "/unused"), LocalFlockLock)


class FakeIdentity:
    """Fake of the ACA identity endpoint and (via RewriteOpener) of IMDS."""

    def __init__(self, clock):
        self.clock, self.requests, self.count = clock, [], 0
        self.status, self.body, self.expiry_style = 200, None, "expires_on"
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                owner.requests.append(
                    (parts.path, urllib.parse.parse_qs(parts.query), {k.lower(): v for k, v in self.headers.items()})
                )
                owner.count += 1
                if owner.status == 307:
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = owner.body
                if body is None:
                    document = {
                        "access_token": f"fake-mi-token-{owner.count}",
                        "token_type": "Bearer",
                        "resource": "https://storage.azure.com/",
                    }
                    if owner.expiry_style == "expires_on":
                        document["expires_on"] = str(int(owner.clock()) + 3600)
                    elif owner.expiry_style == "expires_in":
                        document["expires_in"] = "3600"
                    body = json.dumps(document).encode()
                self.send_response(owner.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.stopped = False
        threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.httpd.shutdown()
            self.httpd.server_close()


class RewriteOpener:
    """Delivers requests aimed at the link-local IMDS address to the fake instead."""

    def __init__(self, base):
        self.base, self.seen = base, []
        self.inner = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, request, timeout=None):
        parts = urllib.parse.urlsplit(request.full_url)
        self.seen.append(parts.netloc)
        forwarded = urllib.request.Request(
            f"{self.base}{parts.path}?{parts.query}",
            data=request.data,
            headers=dict(request.header_items()),
            method=request.get_method(),
        )
        return self.inner.open(forwarded, timeout=timeout)


class ManagedIdentityTokenTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1_700_000_000.0)
        self.identity = FakeIdentity(self.clock)
        self.addCleanup(self.identity.stop)
        self.aca_env = {
            "IDENTITY_ENDPOINT": f"{self.identity.url}/msi/token",
            "IDENTITY_HEADER": "fake-identity-header",
        }

    def provider(self, env=None, **kwargs):
        return ManagedIdentityToken(env={} if env is None else env, now=self.clock, **kwargs)

    def test_container_apps_shape(self):
        token = self.provider(self.aca_env)()
        self.assertEqual(token, "fake-mi-token-1")
        path, query, headers = self.identity.requests[0]
        self.assertEqual(path, "/msi/token")
        self.assertEqual(query["resource"], ["https://storage.azure.com/"])
        self.assertEqual(query["api-version"], ["2019-08-01"])
        self.assertNotIn("client_id", query)
        self.assertEqual(headers["x-identity-header"], "fake-identity-header")
        self.assertNotIn("metadata", headers)

    def test_container_apps_shape_with_client_id_and_custom_resource(self):
        self.provider(self.aca_env, resource="https://vault.azure.net", client_id="client-a")()
        _, query, _ = self.identity.requests[0]
        self.assertEqual(query["client_id"], ["client-a"])
        self.assertEqual(query["resource"], ["https://vault.azure.net"])

    def test_imds_shape_when_no_container_apps_variables(self):
        opener = RewriteOpener(self.identity.url)
        token = self.provider({}, client_id="client-a", opener=opener)()
        self.assertEqual(token, "fake-mi-token-1")
        self.assertEqual(opener.seen, ["169.254.169.254"])
        path, query, headers = self.identity.requests[0]
        self.assertEqual(path, "/metadata/identity/oauth2/token")
        self.assertEqual(query["api-version"], ["2018-02-01"])
        self.assertEqual(query["resource"], ["https://storage.azure.com/"])
        self.assertEqual(query["client_id"], ["client-a"])
        self.assertEqual(headers["metadata"], "true")
        self.assertNotIn("x-identity-header", headers)

    def test_both_container_apps_variables_are_required(self):
        for env in (
            {"IDENTITY_ENDPOINT": self.aca_env["IDENTITY_ENDPOINT"]},
            {"IDENTITY_HEADER": "fake-identity-header"},
        ):
            with self.subTest(env=sorted(env)):
                opener = RewriteOpener(self.identity.url)
                self.provider(env, opener=opener)()
                self.assertEqual(opener.seen, ["169.254.169.254"])

    def test_reads_the_process_environment_by_default(self):
        with patch.dict(os.environ, self.aca_env):
            self.assertEqual(ManagedIdentityToken(now=self.clock)(), "fake-mi-token-1")

    def test_token_is_cached_until_five_minutes_before_expiry(self):
        provider = self.provider(self.aca_env)
        first = provider()
        self.clock.advance(3600 - 301)  # 301 s left: still cached
        self.assertEqual(provider(), first)
        self.assertEqual(self.identity.count, 1)
        self.clock.advance(2)  # 299 s left: refresh
        second = provider()
        self.assertNotEqual(second, first)
        self.assertEqual(self.identity.count, 2)
        self.assertEqual(provider(), second)

    def test_expires_in_is_used_when_expires_on_is_absent(self):
        self.identity.expiry_style = "expires_in"
        provider = self.provider(self.aca_env)
        provider()
        self.clock.advance(1000)
        provider()
        self.assertEqual(self.identity.count, 1)
        self.clock.advance(3000)
        provider()
        self.assertEqual(self.identity.count, 2)

    def test_a_token_without_expiry_is_never_reused(self):
        self.identity.expiry_style = "none"
        provider = self.provider(self.aca_env)
        provider(), provider()
        self.assertEqual(self.identity.count, 2)

    def test_concurrent_callers_share_one_fetch(self):
        provider = self.provider(self.aca_env)
        results = []
        threads = [threading.Thread(target=lambda: results.append(provider())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(set(results), {"fake-mi-token-1"})
        self.assertEqual(self.identity.count, 1)

    def test_failures_raise_token_error_without_leaking_anything_secret(self):
        secret_body = b'{"error": "fake-secret-in-body"}'
        cases = [
            (500, secret_body, "HTTP 500"),
            (404, secret_body, "HTTP 404"),
            (200, b"not json", "not a token document"),
            (200, b'{"expires_on": "1"}', "not a token document"),
            (200, b'{"access_token": 5}', "malformed"),
            (200, b'{"access_token": "a b"}', "malformed"),
            (200, b'{"access_token": "line\\r\\nbreak"}', "malformed"),
            (200, b'{"access_token": "%s"}' % (b"x" * 70000), "too large"),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, expected=expected):
                self.identity.status, self.identity.body = status, body
                with self.assertRaisesRegex(TokenError, expected) as caught:
                    self.provider(self.aca_env)()
                text = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn("fake-secret-in-body", text)
                self.assertNotIn("fake-identity-header", text)

    def test_unreachable_endpoint(self):
        env = dict(self.aca_env)
        self.identity.stop()
        with self.assertRaisesRegex(TokenError, "unreachable"):
            self.provider(env)()

    def test_non_http_endpoint_is_refused_before_any_request(self):
        for endpoint in ("file:///etc/passwd", "ftp://127.0.0.1/x", "/just/a/path"):
            with self.subTest(endpoint=endpoint):
                env = {"IDENTITY_ENDPOINT": endpoint, "IDENTITY_HEADER": "fake-identity-header"}
                with self.assertRaisesRegex(TokenError, "http"):
                    self.provider(env)()
        self.assertEqual(self.identity.count, 0)

    def test_redirects_are_not_followed(self):
        self.identity.status = 307
        with self.assertRaisesRegex(TokenError, "HTTP 307"):
            self.provider(self.aca_env)()
        self.assertEqual(self.identity.count, 1)

    def test_repr_does_not_expose_the_header_secret_or_token(self):
        provider = self.provider(self.aca_env)
        provider()
        self.assertNotIn("fake", repr(provider))

    def test_works_as_the_token_provider_of_a_blob_lock(self):
        identity_token = "fake-mi-token-1"
        server = FakeBlobServer(clock=self.clock, tokens={identity_token}).start()
        self.addCleanup(server.stop)
        provider = self.provider(self.aca_env)
        lock = BlobLeaseLock(
            server.url, CONTAINER, BLOB, provider, clock=self.clock, sleep=Ticker(self.clock).sleep
        )
        self.addCleanup(lock.release)
        lock.acquire()
        self.assertEqual(lock.fencing_token, 1)
        self.assertEqual(self.identity.count, 1)  # cached across the four lock requests
        self.assertTrue(
            all(r.headers["authorization"] == f"Bearer {identity_token}" for r in server.calls())
        )


# --- workload identity ---------------------------------------------------------------

CLIENT_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ASSERTION_SECRET = "fake-assertion-SECRET"


def assertion(n=1):
    """A syntactically valid JWT (three base64url parts) that carries a marker."""
    return f"eyJhbGciOiJSUzI1NiJ9.{ASSERTION_SECRET}-{n}.c2lnbmF0dXJl"


class FakeEntra:
    """Fake of the Microsoft Entra v2 token endpoint (client-assertion flow).

    It checks the request shape, answers with `fake-wi-token-<n>` and keeps every
    request it saw. `status` and `body` override the reply, `fail_next` is a counter.
    """

    def __init__(self, clock, tenant=TENANT_ID):
        self.clock, self.tenant, self.requests, self.count = clock, tenant, [], 0
        self.status, self.body, self.extra = 200, None, {}
        self.token_type, self.fail_next = "Bearer", 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                parts = urllib.parse.urlsplit(self.path)
                owner.requests.append(
                    {
                        "path": parts.path,
                        "query": parts.query,
                        "form": urllib.parse.parse_qs(raw_body.decode(), keep_blank_values=True),
                        "raw": raw_body,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    }
                )
                owner.count += 1
                status, body = owner.status, owner.body
                if owner.fail_next > 0:
                    owner.fail_next -= 1
                    status, body = 503, b"{}"
                if status == 307:
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if body is None:
                    document = {
                        "token_type": owner.token_type,
                        "expires_in": 3599,
                        "ext_expires_in": 3599,
                        "access_token": f"fake-wi-token-{owner.count}",
                        **owner.extra,
                    }
                    body = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.stopped = False
        threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.httpd.shutdown()
            self.httpd.server_close()


class WorkloadIdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(1_700_000_000.0)
        self.entra = FakeEntra(self.clock)
        self.addCleanup(self.entra.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token_file = Path(self.temp.name) / "azure-identity-token"
        self.token_file.write_text(assertion(1))
        self.env = {
            "AZURE_CLIENT_ID": CLIENT_ID,
            "AZURE_TENANT_ID": TENANT_ID,
            "AZURE_FEDERATED_TOKEN_FILE": str(self.token_file),
            "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
        }

    def provider(self, **kwargs):
        """The production URL, delivered to the fake by an opener that rewrites the host."""
        kwargs.setdefault("opener", RewriteOpener(self.entra.url))
        return WorkloadIdentityToken(
            CLIENT_ID, TENANT_ID, self.token_file, now=self.clock, **kwargs
        )

    def local_provider(self, **kwargs):
        """Talks to the fake directly through the loopback test override."""
        return WorkloadIdentityToken(
            CLIENT_ID,
            TENANT_ID,
            self.token_file,
            authority_host=self.entra.url + "/",
            allow_loopback_authority=True,
            now=self.clock,
            **kwargs,
        )

    def text_of(self, caught):
        return "".join(traceback.format_exception(caught.exception))


class WorkloadIdentitySelectionTests(WorkloadIdentityTestCase):
    ACCOUNT = {"FACTORY_LOCK_ACCOUNT_URL": "https://acct.blob.core.windows.net"}

    def lock(self, **extra):
        return lock_from_env("acme__widgets", "/unused", {**self.ACCOUNT, **self.env, **extra})

    def test_selected_when_the_three_variables_exist(self):
        lock = self.lock()
        self.assertIsInstance(lock, BlobLeaseLock)
        provider = lock._token_provider
        self.assertIsInstance(provider, WorkloadIdentityToken)
        self.assertEqual(provider._client_id, CLIENT_ID)
        self.assertEqual(provider._token_file, str(self.token_file))
        self.assertEqual(
            provider._url, f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        )
        self.assertEqual(provider._scope, "https://storage.azure.com/.default")

    def test_authority_host_is_optional_and_defaults_to_public_cloud(self):
        del self.env["AZURE_AUTHORITY_HOST"]
        self.assertTrue(self.lock()._token_provider._url.startswith("https://login.microsoftonline.com/"))
        self.env["AZURE_AUTHORITY_HOST"] = "  "
        self.assertTrue(self.lock()._token_provider._url.startswith("https://login.microsoftonline.com/"))

    def test_sovereign_authority_host_is_honoured(self):
        provider = self.lock(AZURE_AUTHORITY_HOST="https://login.microsoftonline.us")._token_provider
        self.assertEqual(provider._url, f"https://login.microsoftonline.us/{TENANT_ID}/oauth2/v2.0/token")

    def test_explicit_modes(self):
        for mode in ("workload-identity", "WORKLOAD-IDENTITY", " auto "):
            with self.subTest(mode=mode):
                self.assertIsInstance(self.lock(FACTORY_LOCK_AUTH=mode)._token_provider, WorkloadIdentityToken)
        for mode in ("managed-identity", "Managed-Identity"):
            with self.subTest(mode=mode):
                provider = self.lock(FACTORY_LOCK_AUTH=mode)._token_provider
                self.assertIsInstance(provider, ManagedIdentityToken)
                self.assertEqual(provider._client_id, CLIENT_ID)

    def test_managed_identity_path_is_kept(self):
        aca = {"AZURE_CLIENT_ID": CLIENT_ID, "IDENTITY_ENDPOINT": "http://x/msi", "IDENTITY_HEADER": "h"}
        for env in (aca, {"AZURE_CLIENT_ID": CLIENT_ID}, {}, {"AZURE_TENANT_ID": TENANT_ID}):
            with self.subTest(env=sorted(env)):
                lock = lock_from_env("acme__widgets", "/unused", {**self.ACCOUNT, **env})
                self.assertIsInstance(lock._token_provider, ManagedIdentityToken)
        # explicit managed identity wins over workload identity variables, so the ACA path
        # can be forced
        provider = self.lock(FACTORY_LOCK_AUTH="managed-identity", **aca)._token_provider
        self.assertIsInstance(provider, ManagedIdentityToken)

    def test_both_identities_configured_is_ambiguous(self):
        env = {"IDENTITY_ENDPOINT": "http://x/msi", "IDENTITY_HEADER": "fake-identity-header"}
        with self.assertRaisesRegex(LockConfigError, "FACTORY_LOCK_AUTH") as caught:
            self.lock(**env)
        self.assertNotIn("fake-identity-header", str(caught.exception))
        # one of the two ACA variables alone is not an ACA identity, so it is not ambiguous
        self.assertIsInstance(self.lock(IDENTITY_ENDPOINT="http://x/msi")._token_provider, WorkloadIdentityToken)
        # the operator can resolve it either way
        self.assertIsInstance(self.lock(FACTORY_LOCK_AUTH="workload-identity", **env)._token_provider, WorkloadIdentityToken)
        self.assertIsInstance(self.lock(FACTORY_LOCK_AUTH="managed-identity", **env)._token_provider, ManagedIdentityToken)

    def test_partial_configuration_names_what_is_missing(self):
        for missing in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID"):
            with self.subTest(missing=missing):
                env = {k: v for k, v in self.env.items() if k != missing}
                with self.assertRaisesRegex(LockConfigError, missing) as caught:
                    lock_from_env("acme__widgets", "/unused", {**self.ACCOUNT, **env})
                text = str(caught.exception)
                for value in self.env.values():
                    self.assertNotIn(value, text)

    def test_explicit_workload_identity_without_variables_fails_clearly(self):
        env = {**self.ACCOUNT, "FACTORY_LOCK_AUTH": "workload-identity"}
        with self.assertRaises(LockConfigError) as caught:
            lock_from_env("acme__widgets", "/unused", env)
        for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"):
            self.assertIn(name, str(caught.exception))

    def test_unknown_mode_is_refused(self):
        with self.assertRaisesRegex(LockConfigError, "FACTORY_LOCK_AUTH must be one of"):
            self.lock(FACTORY_LOCK_AUTH="msi")

    def test_config_error_is_both_a_lock_error_and_a_value_error(self):
        self.assertTrue(issubclass(LockConfigError, LockError))
        self.assertTrue(issubclass(LockConfigError, ValueError))

    def test_lock_client_id_overrides_azure_client_id(self):
        other = "99999999-8888-7777-6666-555555555555"
        self.assertEqual(self.lock(FACTORY_LOCK_CLIENT_ID=other)._token_provider._client_id, other)
        del self.env["AZURE_CLIENT_ID"]  # FACTORY_LOCK_CLIENT_ID alone satisfies the client id
        self.assertEqual(self.lock(FACTORY_LOCK_CLIENT_ID=other)._token_provider._client_id, other)

    def test_a_given_token_provider_skips_selection_entirely(self):
        provider = lambda: TOKEN  # noqa: E731
        env = {**self.ACCOUNT, "IDENTITY_ENDPOINT": "http://x/msi", "IDENTITY_HEADER": "h", **self.env}
        lock = lock_from_env("acme__widgets", "/unused", env, token_provider=provider)
        self.assertIs(lock._token_provider, provider)

    def test_the_local_lock_ignores_identity_variables(self):
        env = {**self.env, "FACTORY_LOCK_AUTH": "bogus"}
        self.assertIsInstance(lock_from_env("acme__widgets", "/tmp/x.lock", env), LocalFlockLock)

    def test_token_source_from_env_reads_the_process_environment_through_lock_from_env(self):
        with patch.dict(os.environ, {**self.ACCOUNT, **self.env}):
            lock = lock_from_env("acme__widgets", "/unused")
        self.assertIsInstance(lock._token_provider, WorkloadIdentityToken)
        self.assertIsInstance(token_source_from_env({}), ManagedIdentityToken)

    def test_loopback_override_is_only_honoured_for_the_exact_value_one(self):
        env = {**self.env, "AZURE_AUTHORITY_HOST": "http://127.0.0.1:9/"}
        for value in ("", "0", "true", "yes", "11"):
            with self.subTest(value=value), self.assertRaises(LockConfigError):
                WorkloadIdentityToken.from_env({**env, "FACTORY_LOCK_ALLOW_LOOPBACK_AUTHORITY": value})
        provider = WorkloadIdentityToken.from_env({**env, "FACTORY_LOCK_ALLOW_LOOPBACK_AUTHORITY": "1"})
        self.assertEqual(provider._url, f"http://127.0.0.1:9/{TENANT_ID}/oauth2/v2.0/token")


class AuthorityHostTests(unittest.TestCase):
    def test_accepted_hosts_are_normalised(self):
        for value, expected in (
            ("https://login.microsoftonline.com/", "https://login.microsoftonline.com/"),
            ("https://login.microsoftonline.com", "https://login.microsoftonline.com/"),
            ("  https://login.microsoftonline.com/  ", "https://login.microsoftonline.com/"),
            ("HTTPS://LOGIN.MICROSOFTONLINE.COM/", "https://login.microsoftonline.com/"),
            ("https://login.microsoftonline.com:443/", "https://login.microsoftonline.com/"),
            ("https://login.microsoftonline.us/", "https://login.microsoftonline.us/"),
            ("https://login.microsoftonline.de/", "https://login.microsoftonline.de/"),
            ("https://login.partner.microsoftonline.cn/", "https://login.partner.microsoftonline.cn/"),
            ("https://login.chinacloudapi.cn/", "https://login.chinacloudapi.cn/"),
        ):
            with self.subTest(value=value):
                self.assertEqual(validate_authority_host(value), expected)

    def test_everything_else_is_refused_without_echoing_it(self):
        for value in (
            "",
            "   ",
            "login.microsoftonline.com",
            "http://login.microsoftonline.com/",
            "ftp://login.microsoftonline.com/",
            "https://evil.example/",
            "https://login.microsoftonline.com.evil.example/",
            "https://evil.example/login.microsoftonline.com/",
            "https://evilogin.microsoftonline.com/",
            "https://login.microsoftonline.com@evil.example/",
            "https://user-SECRET:pass-SECRET@login.microsoftonline.com/",
            "https://login.microsoftonline.com:8443/",
            "https://login.microsoftonline.com:badport/",
            "https://login.microsoftonline.com/extra/",
            "https://login.microsoftonline.com/?x=1",
            "https://login.microsoftonline.com/#frag",
            "https://login.microsoftonline.com./",
            "https://login.microsoftonline.com\\@evil.example/",
            "https://login.microsoftonline.net/",
            "https://microsoftonline.com/",
            "https://localhost/",
            "http://127.0.0.1:8080/",
            "https://[::1]/",
        ):
            with self.subTest(value=value):
                with self.assertRaises(LockConfigError) as caught:
                    validate_authority_host(value)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertNotIn("evil", str(caught.exception))

    def test_loopback_needs_the_test_flag_and_never_widens_anything_else(self):
        self.assertEqual(validate_authority_host("http://127.0.0.1:8123", True), "http://127.0.0.1:8123/")
        self.assertEqual(validate_authority_host("https://localhost:9/", True), "https://localhost:9/")
        self.assertEqual(validate_authority_host("http://[::1]:7/", True), "http://[::1]:7/")
        for value in (
            "http://evil.example/",
            "http://127.0.0.1.evil.example/",
            "http://127.0.0.2/",
            "http://user:pw@127.0.0.1/",
            "http://127.0.0.1/path/",
            "http://login.microsoftonline.com/",
            "ftp://127.0.0.1/",
        ):
            with self.subTest(value=value), self.assertRaises(LockConfigError):
                validate_authority_host(value, True)


class WorkloadIdentityTokenTests(WorkloadIdentityTestCase):
    def test_request_shape(self):
        opener = RewriteOpener(self.entra.url)
        token = self.provider(opener=opener)()
        self.assertEqual(token, "fake-wi-token-1")
        self.assertEqual(opener.seen, ["login.microsoftonline.com"])
        (request,) = self.entra.requests
        self.assertEqual(request["path"], f"/{TENANT_ID}/oauth2/v2.0/token")
        self.assertEqual(request["query"], "")  # nothing, least of all the assertion, in the URL
        self.assertEqual(
            request["form"],
            {
                "client_id": [CLIENT_ID],
                "scope": ["https://storage.azure.com/.default"],
                "grant_type": ["client_credentials"],
                "client_assertion_type": ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"],
                "client_assertion": [assertion(1)],
            },
        )
        headers = request["headers"]
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(headers["accept"], "application/json")
        self.assertNotIn("authorization", headers)
        self.assertNotIn("metadata", headers)
        self.assertNotIn("x-identity-header", headers)

    def test_the_default_opener_ignores_proxy_variables(self):
        # Requests go straight to the loopback fake even with a proxy that would break them.
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}):
            self.assertEqual(self.local_provider()(), "fake-wi-token-1")

    def test_loopback_override_reaches_the_fake_directly(self):
        self.assertEqual(self.local_provider()(), "fake-wi-token-1")
        self.assertEqual(self.entra.requests[0]["path"], f"/{TENANT_ID}/oauth2/v2.0/token")

    def test_assertion_is_stripped_of_surrounding_whitespace(self):
        self.token_file.write_text(f"\n  {assertion(1)}\r\n")
        self.local_provider()()
        self.assertEqual(self.entra.requests[0]["form"]["client_assertion"], [assertion(1)])

    def test_token_file_is_read_again_on_every_refresh(self):
        provider = self.local_provider()
        provider()
        self.token_file.write_text(assertion(2))
        provider()  # still cached: the rotated file is not looked at, but not needed either
        self.assertEqual(self.entra.count, 1)
        self.clock.advance(3600)
        self.assertEqual(provider(), "fake-wi-token-2")
        self.token_file.write_text(assertion(3))
        self.clock.advance(3600)
        provider()
        sent = [r["form"]["client_assertion"] for r in self.entra.requests]
        self.assertEqual(sent, [[assertion(1)], [assertion(2)], [assertion(3)]])

    def test_rotation_by_atomic_symlink_swap_like_a_projected_volume(self):
        directory = Path(self.temp.name) / "tokens"
        (directory / "..v1").mkdir(parents=True)
        (directory / "..v2").mkdir()
        (directory / "..v1" / "token").write_text(assertion(1))
        (directory / "..v2" / "token").write_text(assertion(2))
        os.symlink("..v1", directory / "..data")
        os.symlink("..data/token", directory / "token")
        provider = WorkloadIdentityToken(
            CLIENT_ID,
            TENANT_ID,
            directory / "token",
            authority_host=self.entra.url,
            allow_loopback_authority=True,
            now=self.clock,
        )
        provider()
        os.symlink("..v2", directory / "..data.tmp")
        os.replace(directory / "..data.tmp", directory / "..data")
        self.clock.advance(3600)
        provider()
        sent = [r["form"]["client_assertion"] for r in self.entra.requests]
        self.assertEqual(sent, [[assertion(1)], [assertion(2)]])

    def test_a_missing_file_recovers_once_it_reappears(self):
        provider = self.local_provider()
        self.token_file.unlink()
        with self.assertRaisesRegex(TokenError, "unreadable"):
            provider()
        self.assertEqual(self.entra.count, 0)
        self.token_file.write_text(assertion(1))
        self.assertEqual(provider(), "fake-wi-token-1")

    def test_token_is_cached_until_five_minutes_before_expiry(self):
        provider = self.local_provider()
        first = provider()
        self.clock.advance(3599 - 301)  # 301 s left: still cached
        self.assertEqual(provider(), first)
        self.assertEqual(self.entra.count, 1)
        self.clock.advance(2)  # 299 s left: refresh
        second = provider()
        self.assertNotEqual(second, first)
        self.assertEqual(self.entra.count, 2)
        self.assertEqual(provider(), second)
        self.assertEqual(self.entra.count, 2)

    def test_expires_on_wins_when_the_endpoint_sends_it(self):
        self.entra.extra = {"expires_on": str(int(self.clock()) + 700)}
        provider = self.local_provider()
        provider()
        self.clock.advance(399)
        provider()
        self.assertEqual(self.entra.count, 1)
        self.clock.advance(2)
        provider()
        self.assertEqual(self.entra.count, 2)

    def test_a_token_without_expiry_is_never_reused(self):
        self.entra.body = json.dumps({"token_type": "Bearer", "access_token": "tok-1"}).encode()
        provider = self.local_provider()
        provider(), provider()
        self.assertEqual(self.entra.count, 2)

    def test_a_short_lived_token_is_never_served_from_the_cache(self):
        self.entra.extra = {"expires_in": 200}
        provider = self.local_provider()
        provider(), provider()
        self.assertEqual(self.entra.count, 2)

    def test_failed_refresh_reuses_a_token_that_is_still_valid(self):
        provider = self.local_provider()
        first = provider()
        self.clock.advance(3599 - 200)  # inside the refresh window, 200 s of life left
        self.entra.status, self.entra.body = 503, b"{}"
        with self.assertLogs("factory.locks", "WARNING") as logs:
            self.assertEqual(provider(), first)
        self.assertEqual(self.entra.count, 2)  # it did try
        self.assertIn("reusing the cached token", "\n".join(logs.output))
        self.clock.advance(171)  # 29 s of life left: below the floor, no more reuse
        with self.assertRaisesRegex(TokenError, "HTTP 503"):
            provider()
        self.entra.status, self.entra.body = 200, None
        self.assertEqual(provider(), "fake-wi-token-4")

    def test_failed_refresh_never_serves_an_expired_token(self):
        provider = self.local_provider()
        provider()
        self.clock.advance(3600)
        self.entra.status, self.entra.body = 500, b"{}"
        with self.assertRaisesRegex(TokenError, "HTTP 500"):
            provider()

    def test_concurrent_callers_share_one_fetch(self):
        provider = self.local_provider()
        results = []
        threads = [threading.Thread(target=lambda: results.append(provider())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(set(results), {"fake-wi-token-1"})
        self.assertEqual(self.entra.count, 1)

    def test_error_reply_reports_only_the_short_codes(self):
        self.entra.status = 401
        self.entra.body = json.dumps(
            {
                "error": "invalid_client",
                "error_description": f"AADSTS700016: fake-secret-in-description {ASSERTION_SECRET}",
                "error_codes": [700016, True, "x", 12345678901234, 7],
                "trace_id": "fake-trace",
            }
        ).encode()
        with self.assertRaises(TokenError) as caught:
            self.local_provider()()
        self.assertEqual(
            str(caught.exception),
            "workload identity endpoint returned HTTP 401 (invalid_client, AADSTS700016, AADSTS7)",
        )
        text = self.text_of(caught)
        for secret in ("fake-secret-in-description", ASSERTION_SECRET, "fake-trace"):
            self.assertNotIn(secret, text)

    def test_failures_raise_token_error_without_leaking_anything_secret(self):
        junk = b'{"error": "fake-secret-in-body"}'
        cases = [
            (500, junk, "HTTP 500"),
            (400, junk, "HTTP 400"),
            (403, b"not json at all fake-secret-in-body", "HTTP 403"),
            (400, b'{"error": "Bad Value <script>"}', "HTTP 400"),
            (200, b"not json", "not a token document"),
            (200, b"[]", "not a token document"),
            (200, b'{"expires_in": 3599}', "not a token document"),
            (200, b'{"access_token": 5}', "malformed"),
            (200, b'{"access_token": "a b"}', "malformed"),
            (200, b'{"access_token": "line\\r\\nbreak"}', "malformed"),
            (200, b'{"access_token": "tok", "token_type": "MAC"}', "not a bearer token"),
            (200, b'{"access_token": "tok", "token_type": 5}', "not a bearer token"),
            (200, b'{"access_token": "%s"}' % (b"x" * 70000), "too large"),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, expected=expected, body=body[:30]):
                self.entra.status, self.entra.body = status, body
                with self.assertLogs("factory.locks", "WARNING") as logs:
                    with self.assertRaisesRegex(TokenError, expected) as caught:
                        self.local_provider()()
                text = self.text_of(caught) + "\n".join(logs.output)
                for secret in ("fake-secret-in-body", ASSERTION_SECRET, "<script>"):
                    self.assertNotIn(secret, text)

    def test_a_token_type_in_any_case_or_absent_is_accepted(self):
        for kind in ("bearer", "BEARER"):
            self.entra.token_type = kind
            self.assertTrue(self.local_provider()())
        self.entra.body = json.dumps({"access_token": "tok-1", "expires_in": 3599}).encode()
        self.assertEqual(self.local_provider()(), "tok-1")

    def test_unreachable_endpoint(self):
        provider = self.local_provider()
        self.entra.stop()
        with self.assertRaisesRegex(TokenError, "unreachable"):
            provider()

    def test_redirects_are_not_followed(self):
        self.entra.status = 307
        with self.assertRaisesRegex(TokenError, "HTTP 307"):
            self.local_provider()()
        self.assertEqual(self.entra.count, 1)

    def test_bad_token_files_are_refused_before_any_request(self):
        for content, expected in (
            (b"", "empty"),
            (b"  \n", "empty"),
            (b"\xff\xfe", "empty or not text"),
            (b"not-a-jwt", "does not hold a JWT"),
            (b"a.b", "does not hold a JWT"),
            (b"a.b.c.d", "does not hold a JWT"),
            (b"a.b.c\nSECRET-line-two", "does not hold a JWT"),
            (b"a b.c.d", "does not hold a JWT"),
            (b"x" * (16 * 1024 + 1), "too large"),
        ):
            with self.subTest(content=content[:20]):
                self.token_file.write_bytes(content)
                with self.assertRaisesRegex(TokenError, expected) as caught:
                    self.local_provider()()
                self.assertNotIn("SECRET-line-two", self.text_of(caught))
        self.assertEqual(self.entra.count, 0)

    def test_unreadable_file_does_not_name_the_path(self):
        self.token_file.write_text(assertion(1))
        self.token_file.chmod(0)
        self.addCleanup(self.token_file.chmod, 0o600)
        if os.access(self.token_file, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        with self.assertRaisesRegex(TokenError, r"unreadable \(PermissionError\)") as caught:
            self.local_provider()()
        self.assertNotIn(str(self.token_file), self.text_of(caught))
        self.assertNotIn("azure-identity-token", self.text_of(caught))

    def test_a_directory_instead_of_a_file(self):
        self.token_file.unlink()
        self.token_file.mkdir()
        with self.assertRaisesRegex(TokenError, "unreadable"):
            self.local_provider()()

    def test_repr_does_not_expose_anything(self):
        provider = self.local_provider()
        provider()
        for text in (repr(provider), str(provider)):
            self.assertEqual(text, "WorkloadIdentityToken(<redacted>)")

    def test_nothing_secret_is_logged_or_written_to_disk(self):
        before = sorted(p.name for p in Path(self.temp.name).iterdir())
        with self.assertLogs("factory.locks", "DEBUG") as logs:
            self.entra.status, self.entra.body = 400, b'{"error": "invalid_client"}'
            with self.assertRaises(TokenError):
                self.local_provider()()
            log_text = "\n".join(logs.output)
        self.assertNotIn(ASSERTION_SECRET, log_text)
        self.assertNotIn(CLIENT_ID, log_text)
        self.entra.status, self.entra.body = 200, None
        self.local_provider()()
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), before)
        self.assertNotIn(ASSERTION_SECRET, self.token_file.read_text().replace(assertion(1), ""))

    def test_construction_validates_every_input(self):
        good = dict(client_id=CLIENT_ID, tenant_id=TENANT_ID, token_file=self.token_file)
        WorkloadIdentityToken(**good)
        WorkloadIdentityToken(**{**good, "tenant_id": "contoso.onmicrosoft.com"})
        for override in (
            {"client_id": ""},
            {"client_id": "client-a"},
            {"client_id": CLIENT_ID + "/x"},
            {"client_id": CLIENT_ID + "&scope=evil"},
            {"tenant_id": ""},
            {"tenant_id": "a/b"},
            {"tenant_id": "../x"},
            {"tenant_id": ".."},
            {"tenant_id": "a?b=c"},
            {"tenant_id": TENANT_ID + "\n"},
            {"token_file": ""},
            {"token_file": "relative/token"},
            {"token_file": "/tmp/x\0y"},
            {"scope": ""},
            {"scope": "a b"},
            {"authority_host": "http://login.microsoftonline.com/"},
            {"authority_host": "https://evil.example/"},
        ):
            with self.subTest(override=override), self.assertRaises(LockConfigError) as caught:
                WorkloadIdentityToken(**{**good, **override})
            self.assertNotIn("evil.example", str(caught.exception))

    def test_tenant_cannot_alter_the_request_path(self):
        provider = WorkloadIdentityToken(CLIENT_ID, "contoso.onmicrosoft.com", self.token_file)
        self.assertEqual(
            provider._url, "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
        )


class WorkloadIdentityLockTests(WorkloadIdentityTestCase):
    """The whole chain: environment -> lock_from_env -> assertion -> token -> blob lease."""

    def test_lock_from_env_acquires_with_the_exchanged_token(self):
        server = FakeBlobServer(clock=self.clock, tokens={"fake-wi-token-1"}).start()
        self.addCleanup(server.stop)
        env = {
            **self.env,
            "FACTORY_LOCK_ACCOUNT_URL": server.url,
            "AZURE_AUTHORITY_HOST": self.entra.url,
            "FACTORY_LOCK_ALLOW_LOOPBACK_AUTHORITY": "1",
        }
        ticker = Ticker(self.clock)
        self.addCleanup(ticker.close)
        lock = lock_from_env(
            "acme__widgets", "/unused", env, clock=self.clock, sleep=ticker.sleep, on_lost=lambda: None
        )
        self.addCleanup(lock.release)
        self.assertIsInstance(lock._token_provider, WorkloadIdentityToken)
        lock._token_provider._now = self.clock
        lock.acquire()
        self.assertEqual(lock.fencing_token, 1)
        self.assertEqual(self.entra.count, 1)  # cached across the lease requests
        self.assertTrue(
            all(r.headers["authorization"] == "Bearer fake-wi-token-1" for r in server.calls())
        )
        # the assertion went to the token endpoint only, never to the blob service
        self.assertNotIn(ASSERTION_SECRET, json.dumps([r.headers for r in server.calls()]))

    def test_a_rejected_assertion_surfaces_as_a_lock_error_without_secrets(self):
        server = FakeBlobServer(clock=self.clock).start()
        self.addCleanup(server.stop)
        self.entra.status = 401
        self.entra.body = b'{"error": "invalid_client", "error_codes": [700024]}'
        lock = BlobLeaseLock(
            server.url, CONTAINER, BLOB, self.local_provider(), clock=self.clock, on_lost=lambda: None
        )
        with self.assertRaises(LockUnavailable) as caught:
            lock.acquire()
        text = self.text_of(caught)
        self.assertIn("TokenError", text)
        self.assertNotIn(ASSERTION_SECRET, text)
        self.assertEqual(server.calls(), [])  # no blob request without a token


class ModuleTests(unittest.TestCase):
    def test_module_is_stdlib_only(self):
        source = Path(locks.__file__).read_text()
        imported = set(re.findall(r"^(?:from|import) ([A-Za-z_][A-Za-z0-9_]*)", source, re.M))
        allowed = {
            "abc", "contextlib", "fcntl", "http", "json", "logging", "os", "re", "threading", "time",
            "urllib", "uuid", "pathlib", "typing", "__future__",
        }
        self.assertEqual(imported - allowed, set())

    def test_error_hierarchy(self):
        for error in (LockBusy, LockLost, LockUnavailable):
            self.assertTrue(issubclass(error, LockError))
            self.assertTrue(issubclass(error, RuntimeError))


if __name__ == "__main__":
    unittest.main()
