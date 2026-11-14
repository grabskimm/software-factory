"""The session record of the pod backend (src/factory/sessionrecord.py): schema, layout and safe file handling.

The supervisor writes the record before it launches a pod and binds the pod name and uid after it; the
relay reads it. Everything runs in temporary directories: no network, no clock, no sleeps.
"""

import ast
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import phases
from factory import podcontract as pc
from factory import sessionrecord as sr

SESSION = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
UID = "11111111-2222-3333-4444-555555555555"
TOKEN = "t" * 43
INSTANCE = "abcdef012345"
POISON = "POISON-9f3a-do-not-echo"


def record(**changes):
    document = sr.build(SESSION, TOKEN, 2_000_000, pc.repo_key("octo/widgets"), "cwd", INSTANCE)
    document.update(changes)
    return document


class Spool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "spool"
        self.root.mkdir()


class Layout(unittest.TestCase):
    def test_the_layout_names_and_modes_are_the_documented_ones(self):
        self.assertEqual(
            (sr.RECORD_FILE, sr.STATE_FILE, sr.HEARTBEAT_FILE, sr.IN_DIR, sr.OUT_DIR, sr.BUNDLE_FILE),
            ("record.json", "state.json", "last_heartbeat", "in", "out", "bundle"),
        )
        self.assertEqual(sr.OUT_FILES, ("transcript", "stderr", "delta", "report.json"))
        self.assertEqual(sr.MARKERS, ("revoke", "released"))
        self.assertEqual(oct(sr.DIR_MODE), "0o2770")
        self.assertEqual(oct(sr.FILE_MODE), "0o660")

    def test_the_caps_never_exceed_the_contract_and_cover_every_cap(self):
        self.assertEqual(
            sr.CAP_LIMITS,
            {
                "bundle_bytes": pc.BUNDLE_MAX_BYTES,
                "delta_bytes": pc.DELTA_MAX_BYTES,
                "delta_files": pc.DELTA_MAX_FILES,
                "file_bytes": pc.FILE_MAX_BYTES,
                "transcript_bytes": pc.TRANSCRIPT_MAX_BYTES,
                "stderr_bytes": pc.STDERR_MAX_BYTES,
            },
        )

    def test_the_write_scopes_are_those_of_the_phase_policies(self):
        scopes = {policy.write_scope for policy in phases.POLICIES.values()}
        self.assertEqual(scopes, set(sr.WRITE_SCOPES))

    def test_the_states_are_the_documented_one_way_machine(self):
        self.assertEqual(
            sr.STATES, ("issued", "bound", "preflighted", "fetched", "streaming", "finished", "expired", "revoked")
        )
        self.assertEqual(sr.TERMINAL_STATES, ("finished", "expired", "revoked"))
        self.assertEqual(sr.MAX_FETCH_ATTEMPTS, 3)

    def test_the_module_is_standard_library_and_the_contract_only(self):
        source = Path(sr.__file__).read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                modules.add("." * node.level + (node.module or ""))
        self.assertTrue(modules <= set(sys.stdlib_module_names) | {"."}, modules)


class Tokens(unittest.TestCase):
    def test_a_token_is_random_urlsafe_and_only_its_hash_is_kept(self):
        first, second = sr.new_token(), sr.new_token()
        self.assertNotEqual(first, second)
        self.assertRegex(first, sr.TOKEN_PATTERN.pattern)
        for token in (first, second):
            self.assertEqual(len(token), 43)
            self.assertEqual(len(base64.urlsafe_b64decode(f"{token}=")), 32)  # 32 random bytes: 256 bits
        self.assertEqual(sr.token_hash(first), hashlib.sha256(first.encode("ascii")).hexdigest())
        self.assertNotIn(first, json.dumps(sr.build(SESSION, first, 5, "0" * 12, "none", INSTANCE)))

    def test_a_malformed_token_is_refused_without_being_echoed(self):
        weaker = secrets.token_urlsafe(24)  # 192 bits, 32 characters: what the pattern used to accept
        for bad in ("", "short", "x" * 42, weaker, "x" * 129, f"{POISON}{'a' * 40} ", "é" * 40, 5, None, b"a" * 40):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError) as caught:
                    sr.token_hash(bad)
                self.assertNotIn(POISON, str(caught.exception))


class Validation(unittest.TestCase):
    def test_a_fresh_record_is_valid_unbound_and_has_exactly_the_closed_fields(self):
        document = record()
        self.assertEqual(set(document), set(sr.RECORD_FIELDS))
        self.assertIsNone(document["pod_name"])
        self.assertIsNone(document["pod_uid"])
        self.assertEqual(document["caps"], sr.default_caps())
        self.assertEqual(sr.validate(document), document)

    def test_extra_and_missing_fields_are_refused(self):
        extra = dict(record(), surprise=1)
        missing = {key: value for key, value in record().items() if key != "deadline"}
        for bad in (extra, missing, [], None, "text", 5):
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(sr.RecordError):
                    sr.validate(bad)

    def test_every_field_rejects_bad_values_and_a_boolean_is_not_a_number(self):
        cases = {
            "version": (2, True, 0, "1"),
            "session": ("", SESSION.upper(), SESSION[:-1], f"{SESSION}0", "../../etc/passwd", None, 7),
            "token_sha256": ("a" * 63, "A" * 64, "g" * 64, None),
            "deadline": (0, -1, True, 1.5, "5", 2**41, None),
            "repo": ("a" * 11, "A" * 12, None),
            "write_scope": ("all", "", None, "CWD"),
            "manager": ("a" * 13, "ZZZZZZZZZZZZ", None),
            "caps": ({}, [], None, "x"),
        }
        for field, values in cases.items():
            for bad in values:
                with self.subTest(field=field, bad=repr(bad)[:30]):
                    with self.assertRaises(sr.RecordError):
                        sr.validate(record(**{field: bad}))

    def test_a_pod_is_bound_as_a_pair_with_the_contract_pod_name_for_this_session(self):
        name = pc.pod_name(SESSION)
        self.assertEqual(sr.validate(record(pod_name=name, pod_uid=UID))["pod_uid"], UID)
        bad_pairs = (
            (name, None),
            (None, UID),
            (pc.pod_name(OTHER), UID),
            ("factory-w-x", UID),
            (name, "UPPER-CASE-UID-123"),
            (name, "short"),
            (name, "../../etc/passwd"),
            (name, 12345678),
        )
        for pod_name, pod_uid in bad_pairs:
            with self.subTest(pod_name=pod_name, pod_uid=pod_uid):
                with self.assertRaises(sr.RecordError):
                    sr.validate(record(pod_name=pod_name, pod_uid=pod_uid))

    def test_caps_must_be_complete_positive_integers_within_the_contract(self):
        for key, limit in sr.CAP_LIMITS.items():
            good = dict(sr.default_caps(), **{key: 1})
            self.assertEqual(sr.validate(record(caps=good))["caps"][key], 1)
            for bad in (0, -1, limit + 1, True, 1.0, "1", None):
                with self.subTest(key=key, bad=repr(bad)):
                    with self.assertRaises(sr.RecordError):
                        sr.validate(record(caps=dict(sr.default_caps(), **{key: bad})))
        with self.assertRaises(sr.RecordError):
            sr.validate(record(caps=dict(sr.default_caps(), extra=1)))
        with self.assertRaises(sr.RecordError):
            sr.validate(record(caps={key: 1 for key in list(sr.CAP_LIMITS)[:-1]}))

    def test_an_error_never_echoes_the_rejected_value(self):
        for field in ("session", "token_sha256", "repo", "write_scope", "manager", "deadline"):
            with self.subTest(field=field):
                with self.assertRaises(sr.RecordError) as caught:
                    sr.validate(record(**{field: POISON}))
                self.assertNotIn(POISON, str(caught.exception))
        with self.assertRaises(sr.RecordError) as caught:
            sr.validate(dict(record(), **{POISON: 1}))
        self.assertNotIn(POISON, str(caught.exception))

    def test_validate_returns_a_copy_and_build_refuses_a_bad_token(self):
        document = record()
        copy = sr.validate(document)
        copy["caps"]["bundle_bytes"] = 1
        self.assertNotEqual(document["caps"]["bundle_bytes"], 1)
        with self.assertRaises(ValueError):
            sr.build(SESSION, "short", 5, "0" * 12, "none", INSTANCE)


class StateValidation(unittest.TestCase):
    def test_a_new_state_is_valid_and_closed(self):
        self.assertEqual(sr.validate_state(sr.new_state()), sr.new_state())
        self.assertEqual(set(sr.new_state()), set(sr.STATE_FIELDS))

    def test_bad_state_documents_are_refused(self):
        base = sr.new_state()
        bad_documents = [
            dict(base, state="weird"),
            dict(base, state="released"),
            dict(base, fetch_attempts=sr.MAX_FETCH_ATTEMPTS + 1),
            dict(base, fetch_attempts=True),
            dict(base, fetch_attempts=-1),
            dict(base, reject="made_up"),
            dict(base, preflight_code="UPPER"),
            dict(base, preflight_code="x" * 33),
            dict(base, preflight_code=5),
            dict(base, ended_at=-1),
            dict(base, ended_at=False),
            dict(base, version=2),
            dict(base, extra=1),
            {key: value for key, value in base.items() if key != "state"},
            [],
            None,
        ]
        for bad in bad_documents:
            with self.subTest(bad=repr(bad)[:60]):
                with self.assertRaises(sr.RecordError):
                    sr.validate_state(bad)

    def test_every_reject_code_and_state_is_accepted(self):
        for code in sr.REJECT_CODES:
            self.assertEqual(sr.validate_state(dict(sr.new_state(), reject=code))["reject"], code)
        for state in sr.STATES:
            self.assertEqual(sr.validate_state(dict(sr.new_state(), state=state))["state"], state)


class Files(Spool):
    def test_the_supervisor_side_write_is_read_back_by_the_descriptor_functions(self):
        document = record()
        sr.write(self.root, document)
        self.assertEqual(sr.read(self.root, SESSION), document)
        with sr.session_dir(self.root, SESSION) as fd:
            self.assertEqual(sr.read_at(fd), document)
        # An integer descriptor for the root works as well as a path (the relay passes its own).
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        self.assertEqual(sr.read(root_fd, SESSION), document)

    def test_files_and_directories_have_the_documented_modes_whatever_the_umask(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        sr.write(self.root, record())
        with sr.session_dir(self.root, SESSION) as fd:
            sr.write_state_at(fd, sr.new_state())
            sr.write_heartbeat_at(fd, 5)
            sr.mark_at(fd, sr.MARKER_REVOKE)
            out_fd = sr.ensure_dir_at(fd, sr.OUT_DIR)
            os.close(out_fd)
        directory = self.root / SESSION
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), sr.DIR_MODE)
        self.assertEqual(stat.S_IMODE((directory / "out").stat().st_mode), sr.DIR_MODE)
        for name in (sr.RECORD_FILE, sr.STATE_FILE, sr.HEARTBEAT_FILE, sr.MARKER_REVOKE):
            with self.subTest(name=name):
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), sr.FILE_MODE)
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["last_heartbeat", "out", "record.json", "revoke", "state.json"])

    def test_a_failed_rewrite_keeps_the_old_record_and_leaves_no_temporary_file(self):
        sr.write(self.root, record())
        before = (self.root / SESSION / sr.RECORD_FILE).read_bytes()
        with mock.patch.object(sr.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sr.write(self.root, record(deadline=3_000_000))
        self.assertEqual((self.root / SESSION / sr.RECORD_FILE).read_bytes(), before)
        self.assertEqual(sorted(path.name for path in (self.root / SESSION).iterdir()), [sr.RECORD_FILE])

    def test_a_failed_first_write_leaves_no_record_at_all(self):
        with mock.patch.object(sr.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sr.write(self.root, record())
        with self.assertRaises(sr.MissingRecord):
            sr.read(self.root, SESSION)
        self.assertEqual(list((self.root / SESSION).iterdir()), [])

    def test_a_reader_never_sees_a_partial_record(self):
        sr.write(self.root, record())
        seen = []
        real = os.replace

        def replace(source, destination, **keywords):
            # At the instant before the rename the old record must still be complete and valid.
            seen.append(sr.read(self.root, SESSION)["deadline"])
            real(source, destination, **keywords)

        with mock.patch.object(sr.os, "replace", replace):
            sr.write(self.root, record(deadline=3_000_000))
        self.assertEqual(seen, [2_000_000])
        self.assertEqual(sr.read(self.root, SESSION)["deadline"], 3_000_000)

    def test_a_session_that_has_no_record_is_missing_and_a_bad_id_is_refused(self):
        with self.assertRaises(sr.MissingRecord):
            sr.read(self.root, SESSION)
        for bad in ("../x", "x" * 32, SESSION.upper(), "", f"{SESSION}/..", "..", None):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(sr.RecordError):
                    sr.read(self.root, bad)
        with self.assertRaises(sr.RecordError):
            sr.write(self.root, dict(record(), session="../../evil"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_unreadable_record_is_a_record_error_not_a_crash(self):
        sr.write(self.root, record())
        path = self.root / SESSION / sr.RECORD_FILE
        for content in (b"", b"not json", b"[1, 2]", b'{"version": 1}', b"\xff\xfe", b"[" * 5000, b"".join([b'{"a": "', b"x" * 5000])):
            with self.subTest(content=content[:20]):
                path.write_bytes(content)
                with self.assertRaises(sr.RecordError):
                    sr.read(self.root, SESSION)

    def test_a_record_with_a_field_added_on_disk_is_refused_on_read(self):
        sr.write(self.root, record())
        path = self.root / SESSION / sr.RECORD_FILE
        document = json.loads(path.read_text(encoding="utf-8"))
        document["extra"] = 1
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(sr.RecordError):
            sr.read(self.root, SESSION)

    def test_a_link_or_a_fifo_in_place_of_a_file_is_refused_and_never_followed_or_waited_on(self):
        sr.write(self.root, record())
        directory = self.root / SESSION
        decoy = Path(self.tmp.name) / "decoy.json"
        decoy.write_text(json.dumps(record()), encoding="utf-8")
        (directory / sr.RECORD_FILE).unlink()
        (directory / sr.RECORD_FILE).symlink_to(decoy)
        with self.assertRaises(sr.RecordError):
            sr.read(self.root, SESSION)
        (directory / sr.RECORD_FILE).unlink()
        os.mkfifo(directory / sr.RECORD_FILE)
        with self.assertRaises(sr.RecordError):
            sr.read(self.root, SESSION)

    def test_a_link_in_place_of_the_session_directory_is_refused(self):
        decoy = Path(self.tmp.name) / "elsewhere"
        decoy.mkdir()
        (self.root / SESSION).symlink_to(decoy)
        with self.assertRaises(OSError):
            with sr.session_dir(self.root, SESSION):
                self.fail("a linked session directory must not open")
        with self.assertRaises(OSError):
            sr.write(self.root, record())
        self.assertEqual(list(decoy.iterdir()), [])

    def test_ensure_dir_refuses_a_link_and_a_file_and_reuses_a_directory(self):
        sr.write(self.root, record())
        directory = self.root / SESSION
        with sr.session_dir(self.root, SESSION) as fd:
            first = sr.ensure_dir_at(fd, sr.OUT_DIR)
            second = sr.ensure_dir_at(fd, sr.OUT_DIR)
            os.close(first)
            os.close(second)
            (directory / "in").symlink_to(self.tmp.name)
            with self.assertRaises(OSError):
                sr.ensure_dir_at(fd, "in")
            (directory / "file").write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                sr.ensure_dir_at(fd, "file")


class Binding(Spool):
    def test_bind_sets_the_pod_name_and_uid_atomically_and_keeps_the_rest(self):
        sr.write(self.root, record())
        bound = sr.bind(self.root, SESSION, pc.pod_name(SESSION), UID)
        self.assertEqual((bound["pod_name"], bound["pod_uid"]), (pc.pod_name(SESSION), UID))
        self.assertEqual(sr.read(self.root, SESSION), bound)
        self.assertEqual(bound["token_sha256"], record()["token_sha256"])

    def test_binding_the_same_pod_again_is_allowed_another_pod_is_not(self):
        sr.write(self.root, record())
        sr.bind(self.root, SESSION, pc.pod_name(SESSION), UID)
        sr.bind(self.root, SESSION, pc.pod_name(SESSION), UID)
        with self.assertRaises(sr.RecordError):
            sr.bind(self.root, SESSION, pc.pod_name(SESSION), "99999999-2222-3333-4444-555555555555")
        self.assertEqual(sr.read(self.root, SESSION)["pod_uid"], UID)

    def test_a_pod_name_of_another_session_or_a_bad_uid_cannot_be_bound(self):
        sr.write(self.root, record())
        for name, uid in ((pc.pod_name(OTHER), UID), (pc.pod_name(SESSION), "BAD"), (None, None)):
            with self.subTest(name=name, uid=uid):
                with self.assertRaises(sr.RecordError):
                    sr.bind(self.root, SESSION, name, uid)
        self.assertIsNone(sr.read(self.root, SESSION)["pod_uid"])

    def test_binding_a_session_without_a_record_is_missing(self):
        with self.assertRaises(sr.MissingRecord):
            sr.bind(self.root, SESSION, pc.pod_name(SESSION), UID)


class StateHeartbeatMarkers(Spool):
    def setUp(self):
        super().setUp()
        sr.write(self.root, record())

    def test_an_absent_state_is_a_fresh_one_and_a_written_one_round_trips(self):
        self.assertEqual(sr.read_state(self.root, SESSION), sr.new_state())
        with sr.session_dir(self.root, SESSION) as fd:
            sr.write_state_at(fd, dict(sr.new_state(), state="fetched", fetch_attempts=2))
        self.assertEqual(sr.read_state(self.root, SESSION)["state"], "fetched")
        self.assertEqual(sr.read_state(self.root, SESSION)["fetch_attempts"], 2)

    def test_a_state_that_is_not_valid_is_refused_on_write_and_read(self):
        with sr.session_dir(self.root, SESSION) as fd:
            with self.assertRaises(sr.RecordError):
                sr.write_state_at(fd, dict(sr.new_state(), state="weird"))
        (self.root / SESSION / sr.STATE_FILE).write_text("{}", encoding="utf-8")
        with self.assertRaises(sr.RecordError):
            sr.read_state(self.root, SESSION)

    def test_the_heartbeat_is_an_integer_and_anything_else_reads_as_none(self):
        self.assertIsNone(sr.read_heartbeat(self.root, SESSION))
        with sr.session_dir(self.root, SESSION) as fd:
            sr.write_heartbeat_at(fd, 1_700_000_000)
        self.assertEqual(sr.read_heartbeat(self.root, SESSION), 1_700_000_000)
        self.assertEqual((self.root / SESSION / sr.HEARTBEAT_FILE).read_text(encoding="ascii"), "1700000000\n")
        for content in (b"abc", b"1.5", b"-3", b"", b"1" * 40, b"\xff"):
            with self.subTest(content=content):
                (self.root / SESSION / sr.HEARTBEAT_FILE).write_bytes(content)
                self.assertIsNone(sr.read_heartbeat(self.root, SESSION))
        with sr.session_dir(self.root, SESSION) as fd:
            for bad in (True, -1, 1.5, "5", None):
                with self.subTest(bad=repr(bad)):
                    with self.assertRaises(sr.RecordError):
                        sr.write_heartbeat_at(fd, bad)

    def test_markers_are_created_once_empty_and_unknown_names_are_refused(self):
        for name in sr.MARKERS:
            self.assertFalse(sr.is_marked(self.root, SESSION, name))
            sr.mark(self.root, SESSION, name)
            sr.mark(self.root, SESSION, name)
            self.assertTrue(sr.is_marked(self.root, SESSION, name))
            self.assertEqual((self.root / SESSION / name).read_bytes(), b"")
        with sr.session_dir(self.root, SESSION) as fd:
            for bad in ("record.json", "../revoke", "", "REVOKE"):
                with self.subTest(bad=bad):
                    with self.assertRaises(sr.RecordError):
                        sr.marked_at(fd, bad)
                    with self.assertRaises(sr.RecordError):
                        sr.mark_at(fd, bad)
                    with self.assertRaises(sr.RecordError):
                        sr.marker_time_at(fd, bad)

    def test_a_marker_is_present_even_as_a_dangling_link_and_the_link_is_not_followed(self):
        (self.root / SESSION / sr.MARKER_REVOKE).symlink_to(Path(self.tmp.name) / "nowhere")
        self.assertTrue(sr.is_marked(self.root, SESSION, sr.MARKER_REVOKE))
        with sr.session_dir(self.root, SESSION) as fd:
            self.assertIsNotNone(sr.marker_time_at(fd, sr.MARKER_REVOKE))
            self.assertIsNone(sr.marker_time_at(fd, sr.MARKER_RELEASED))


if __name__ == "__main__":
    unittest.main()
