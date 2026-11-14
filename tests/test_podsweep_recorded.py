"""A directory that is gone is not an empty set of owners (``Operations.recorded_pods``, slice 20 of the pod backend).

The sweep deletes a pod nobody owns. A config mount or a data volume that is briefly absent must therefore read as an
INCOMPLETE answer (nothing is deleted for want of an owner), never as a complete answer with no owner at all. Every
case runs on the fakes of tests/test_operations.py and a scripted launcher client; nothing sleeps, no pod exists.
"""

import json
import shutil
import unittest

from factory import podcontract as pc
from factory import podsweep
from tests import test_operations as base
from tests.test_podsweep import Stub, handle, listing

NOW = int(base.NOW)


class DirectoryCase(base.OpsCase):
    def setUp(self):
        super().setUp()
        podsweep.reset()
        self.addCleanup(podsweep.reset)

    def own(self, status="running"):
        directory = self.make_run()
        (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": {"job-1": {"status": status, "pod": handle(1).record()}}}))

    def sweep(self, *pods):
        stub = Stub(list(pods))
        self.manager.pods = podsweep.Sweeper(stub.list_pods, stub.delete_pod, None, clock=self.time.clock)
        self.ops.pod_sweep_pass()
        return stub


class MissingDirectoryTests(DirectoryCase):
    def old_pod(self):
        return listing(2, created=NOW - 10 * pc.LAUNCH_GRACE_SECONDS, deadline=NOW + 3600)

    def test_a_missing_config_repos_directory_is_incomplete_and_deletes_nothing(self):
        self.own()
        shutil.rmtree(self.config_dir / "repos")
        self.assertFalse(self.ops.recorded_pods().complete)
        stub = self.sweep(self.old_pod())
        self.assertEqual(stub.deleted, [])
        self.assertEqual(self.lines(), ["pod sweep deleted=0 released=0 failed=0 incomplete=1 unreadable=0"])

    def test_a_config_repos_directory_that_is_a_file_is_incomplete_too(self):
        shutil.rmtree(self.config_dir / "repos")
        (self.config_dir / "repos").write_text("not a directory")
        self.assertFalse(self.ops.recorded_pods().complete)

    def test_a_missing_data_volume_is_incomplete_and_deletes_nothing(self):
        self.own()
        shutil.rmtree(self.data_dir / "repos")
        self.assertFalse(self.ops.recorded_pods().complete)
        stub = self.sweep(self.old_pod())
        self.assertEqual(stub.deleted, [])
        self.assertEqual(self.lines(), ["pod sweep deleted=0 released=0 failed=0 incomplete=1 unreadable=0"])

    def test_a_repository_whose_runs_were_purged_while_the_volume_is_up_has_no_owners_and_that_is_complete(self):
        # The purge removed the run (and the job that owned the pod with it): nothing owns the pod, which is true.
        self.own()
        shutil.rmtree(self.data_dir / "repos" / "org" / "name" / "runs")
        found = self.ops.recorded_pods()
        self.assertEqual((found.complete, found.entries), (True, {}))

    def test_a_configured_repository_that_never_ran_is_fine_while_the_volume_is_there(self):
        self.own()
        self.assertFalse((self.data_dir / "repos" / "org" / "other").exists())
        self.assertTrue(self.ops.recorded_pods().complete)

    def test_with_everything_present_an_unowned_pod_is_still_deleted(self):
        self.own()
        stub = self.sweep(self.old_pod())
        self.assertEqual([name for name, _uid, _revoked in stub.deleted], [listing(2)["name"]])
        self.assertEqual(self.lines(), ["pod sweep deleted=1 released=0 failed=0 incomplete=0 unreadable=0"])


if __name__ == "__main__":
    unittest.main()
