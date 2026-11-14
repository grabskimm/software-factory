"""tools/live_recording.py: the recording format of the live runbook (slice 21 of the pod backend, docs/pod-backend-runbook.md).

What these tests prove: a recording has a closed shape; a payload that holds a Kubernetes Secret is refused and every credential
shape, GUID and address is scrubbed before anything is written; a recording edited by hand fails `validate`; the file is written once,
with mode 0600. They do not prove the scrubber finds every secret (it is a safety net: the runbook says so and has the owner review a
recording before it is copied into the repository).
"""

import copy
import datetime
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests.live_tools import load

rec = load("live_recording")

CLOCK = lambda: datetime.datetime(2026, 11, 12, 10, 0, 0, tzinfo=datetime.timezone.utc)  # noqa: E731
POD = {
    "kind": "Pod",
    "metadata": {"name": "factory-w-abc", "uid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "managedFields": [{"manager": "x"}]},
    "spec": {"containers": [{"name": "c", "env": [{"name": "ANTHROPIC_API_KEY", "value": "sk-ant-api03-abcdefghij"}, {"name": "FACTORY_NODE_IP", "value": "10.1.2.3"}]}]},
    "status": {"phase": "Pending", "hostIP": "10.0.0.4", "message": "0/3 nodes are available: 3 Insufficient cpu."},
}


def made(**override):
    options = {"step": "D5", "kind": "pod", "payload": POD, "argv": ["kubectl", "get", "pod", "x", "-o", "json"], "returncode": 0, "clock": CLOCK}
    options.update(override)
    return rec.make(**options)


class Shape(unittest.TestCase):
    def test_a_recording_has_exactly_the_documented_keys_and_validates(self):
        document = made()
        self.assertEqual(sorted(document), sorted(rec.KEYS))
        self.assertEqual(rec.problems(document), [])
        self.assertEqual(document["recorded_at"], "2026-11-12T10:00:00Z")
        self.assertEqual(document["format"], "factory-runbook-recording/1")

    def test_the_vocabulary_of_kinds_is_closed_and_each_has_a_form(self):
        for kind, (form, purpose) in rec.KINDS.items():
            self.assertIn(form, ("json", "text", "either"), kind)
            self.assertTrue(purpose.strip(), kind)
        with self.assertRaises(rec.Refused) as caught:
            made(kind="anything")
        self.assertEqual(caught.exception.code, "kind")

    def test_a_step_is_a_part_letter_a_to_g_and_a_number(self):
        for step in ("A1", "G12", "D5"):
            self.assertEqual(made(step=step)["step"], step)
        for step in ("H1", "D", "d5", "D123", "5D", "", None, "D5; rm"):
            with self.subTest(step=step), self.assertRaises(rec.Refused):
                made(step=step)

    def test_the_form_of_a_kind_is_enforced(self):
        with self.assertRaises(rec.Refused):
            made(kind="denial", payload={"a": 1})  # a text kind takes text
        with self.assertRaises(rec.Refused):
            made(kind="pod", payload="plain text")  # a json kind takes a document
        text = made(kind="denial", payload="ValidatingAdmissionPolicy 'x' denied request: [rule:image] no", argv=None, returncode=None)
        self.assertEqual(text["payload"], {"text": "ValidatingAdmissionPolicy 'x' denied request: [rule:image] no"})
        either = made(kind="kube-status", payload="Error from server (AlreadyExists): configmaps \"x\" already exists", argv=None)
        self.assertEqual(rec.problems(either), [])

    def test_the_argv_must_be_a_short_list_of_clean_strings(self):
        for argv in ("kubectl get", [], [1], ["x"] * 41, ["x" * (rec.MAX_ARGUMENT + 1)], ["curl", "-H", "Authorization: Bearer abcdefghijklmnop"], ["login", "--token", "abc123"], ["login", "--password=abc123"]):
            with self.subTest(argv=argv), self.assertRaises(rec.Refused):
                made(argv=argv)

    def test_the_payload_has_a_size_bound(self):
        with self.assertRaises(rec.Refused) as caught:
            made(kind="note", payload="x" * (rec.MAX_PAYLOAD_BYTES + 1), argv=None, returncode=None)
        self.assertEqual(caught.exception.code, "size")


class Scrubbing(unittest.TestCase):
    def test_credentials_guids_addresses_and_environment_values_are_replaced(self):
        document = made()
        text = json.dumps(document)
        for leaked in ("sk-ant-api03", "3f2504e0", "10.1.2.3", "10.0.0.4"):
            self.assertNotIn(leaked, text)
        env = document["payload"]["spec"]["containers"][0]["env"]
        self.assertEqual(env[0]["value"], rec.REDACTED)
        self.assertEqual(document["payload"]["metadata"]["uid"], rec.ZERO_GUID)
        self.assertNotIn("managedFields", document["payload"]["metadata"])
        self.assertEqual(document["payload"]["status"]["message"], "0/3 nodes are available: 3 Insufficient cpu.")  # shapes survive
        self.assertGreaterEqual(document["redactions"], 5)

    def test_each_credential_shape_is_found(self):
        shapes = {
            "an Anthropic key": "key is sk-ant-api03-AbCdEfGhIjKl",
            "a GitHub token": "token ghs_AbCdEfGhIjKlMnOpQrSt",
            "a fine-grained token": "github_pat_11ABCDEFG0abcdefghijklmn",
            "a JWT": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmn",
            "a bearer": "Authorization: Bearer abcdef0123456789",
            "a SAS signature": "https://x.blob.core.windows.net/c?sv=2020&sig=AbCdEf%2B012345678",
            "a private key": "-----BEGIN RSA PRIVATE KEY-----",
            "an opaque blob": "QWxhZGRpbjpvcGVuIHNlc2FtZVF3ZXJ0eVVpb3BBc2RmZ2hqa2w5ODc2NTQzMg==",
        }
        for name, text in shapes.items():
            with self.subTest(name):
                cleaned, count = rec.scrub_text(text)
                self.assertGreaterEqual(count, 1, name)
                self.assertNotEqual(cleaned, text)

    def test_what_a_fixture_needs_is_kept(self):
        kept = {
            "an image digest": f"registry.example.invalid/factory@sha256:{'ab' * 32}",
            "the metadata address": "169.254.169.254",
            "the Azure host address": "168.63.129.16",
            "a kernel version": "6.6.159.1-2.azl3",
            "an API path": "/api/v1/namespaces/factory-workers/pods/factory-w-0123456789ab",
            "a label key": "karpenter.sh/capacity-type",
        }
        for name, text in kept.items():
            with self.subTest(name):
                self.assertEqual(rec.scrub_text(text), (text, 0))
        pool = {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["on-demand"]}
        self.assertEqual(rec.scrub(pool), (pool, 0))  # a field called `key` is not a secret

    def test_a_json_key_that_names_a_secret_loses_its_value_but_a_name_does_not(self):
        cleaned, count = rec.scrub({"token": "abc", "clientSecret": "def", "secretName": "factory-w-x-s", "apiKey": "ghi"})
        self.assertEqual(cleaned, {"token": rec.REDACTED, "clientSecret": rec.REDACTED, "secretName": "factory-w-x-s", "apiKey": rec.REDACTED})
        self.assertEqual(count, 3)

    def test_a_kubernetes_secret_is_refused_wherever_it_is_never_scrubbed(self):
        secret = {"apiVersion": "v1", "kind": "Secret", "data": {"token": "cGxhY2Vob2xkZXI="}}
        for payload in (secret, {"kind": "List", "items": [POD, secret]}, [{"nested": {"deeper": secret}}]):
            with self.subTest(payload=str(payload)[:30]), self.assertRaises(rec.Refused) as caught:
                made(payload=payload)
            self.assertEqual(caught.exception.code, "secret-object")

    def test_a_recording_with_an_unscrubbed_value_does_not_validate(self):
        document = made()
        document["payload"]["status"]["leak"] = "sk-ant-api03-abcdefghij"
        document["sha256"] = rec.digest(document["payload"])  # even with a matching hash
        self.assertEqual(rec.problems(document), ["unscrubbed"])

    def test_the_value_of_a_sensitive_environment_entry_is_replaced_whatever_it_looks_like(self):
        # `hunter2` has no credential shape: only the NAME of the entry says it is one. (The key of the fixture above is caught by the
        # key pattern as well, so it proves nothing about this rule.)
        cases = {"DB_PASSWORD": "hunter2", "GITHUB_APP_PRIVATE_KEY": "plain words", "SESSION_TOKEN": "12345", "CLIENT_SECRET": "x", "AUTHORIZATION": "Basic", "API_CREDENTIAL": "y"}
        for name, value in cases.items():
            with self.subTest(name):
                cleaned, count = rec.scrub({"env": [{"name": name, "value": value}]})
                self.assertEqual(cleaned, {"env": [{"name": name, "value": rec.REDACTED}]})
                self.assertEqual(count, 1)
        harmless = {"env": [{"name": "FACTORY_NODE_IP_NAME", "value": "hunter2"}, {"name": "LOG_LEVEL", "value": "debug"}]}
        self.assertEqual(rec.scrub(harmless), (harmless, 0))
        self.assertEqual(rec.scrub({"env": [{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]})[1], 0)  # a reference is a shape to keep

    def test_a_secret_written_as_text_a_name_value_pair_or_a_flag_is_replaced_in_every_kind_of_text(self):
        cases = {
            "password: hunter2": "password: " + rec.REDACTED,
            "secret=abcdef": "secret=" + rec.REDACTED,
            "token: kubeconfigtoken123": "token: " + rec.REDACTED,
            "export DB_PASSWORD=hunter2": "export DB_PASSWORD=" + rec.REDACTED,
            "--token=abc123def --other=1": "--token=" + rec.REDACTED + " --other=1",
            "client_secret = 'two words'": "client_secret = " + rec.REDACTED,
            '{"apiKey": "abc"}': '{"apiKey": ' + rec.REDACTED + "}",
            "authorization: Basic dXNlcjpwYXNz": "authorization: " + rec.REDACTED,
            "https://x.blob.core.windows.net/c?sv=1&sig=AbCdEf012345678": "https://x.blob.core.windows.net/c?sv=1&" + rec.REDACTED,
            "sig=abcdefghijkl": rec.REDACTED,
            "a" * 40: rec.REDACTED,
            "fe80::1 2001:db8:85a3:0:0:8a2e:370:7334 ::1": ":: :: ::",
        }
        for text, want in cases.items():
            with self.subTest(text):
                for kind in ("note", "probe-output", "claude-result", "plan-actions"):
                    document = made(kind=kind, payload=text, argv=None, returncode=None)
                    self.assertEqual(document["payload"], {"text": want}, kind)
                    self.assertGreaterEqual(document["redactions"], 1)
                    self.assertEqual(rec.problems(document), [])  # and what is left is clean: a second scrub changes nothing

    def test_a_flag_whose_next_word_is_the_secret_loses_that_word(self):
        cleaned, count = rec.scrub({"args": ["--token", "abc", "--api-key", "k", "--secret-name", "n", "--verbose", "--password", "--x"]})
        self.assertEqual(cleaned, {"args": ["--token", rec.REDACTED, "--api-key", rec.REDACTED, "--secret-name", "n", "--verbose", "--password", "--x"]})
        self.assertEqual(count, 2)

    def test_what_a_fixture_needs_survives_the_text_rules(self):
        kept = (
            f"registry.example.invalid/factory@sha256:{'ab' * 32}",
            f"containerd image sha256:{'cd' * 32}",
            "2026-11-09T12:34:56Z at 12:34:56",
            "token: " + rec.REDACTED,
            "secretName: factory-w-x-s",
            "kind: SecretProviderClass",
            "design=abcd1234 (a name that merely ends in sig)",
            "Insufficient cpu; aa:bb:cc:dd:ee:ff",
            "Namespace::Name and a Terraform address module.x::y",
        )
        for text in kept:
            with self.subTest(text):
                cleaned, count = rec.scrub_text(text)
                self.assertEqual((cleaned, count), (text, 0))

    def test_a_secret_list_or_a_secret_inside_a_string_is_refused_never_scrubbed(self):
        annotated = {"metadata": {"annotations": {"kubectl.kubernetes.io/last-applied-configuration": '{"apiVersion":"v1","kind":"Secret","data":{"a":"Yg=="}}'}}}
        yaml_text = "apiVersion: v1\nkind: Secret\ndata:\n  a: Yg==\n"
        for payload in ({"kind": "SecretList", "items": []}, {"x": {"kind": "SecretList"}}, annotated, [yaml_text]):
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(rec.Refused) as caught:
                made(payload=payload)
            self.assertEqual(caught.exception.code, "secret-object")
        for text in (yaml_text, 'the object {"kind": "Secret"}'):
            with self.assertRaises(rec.Refused):
                made(kind="note", payload=text, argv=None, returncode=None)
        # a name that merely starts with the word is a different kind
        self.assertEqual(rec.scrub({"kind": "SecretProviderClass", "note": "kind: SecretProviderClass"}), ({"kind": "SecretProviderClass", "note": "kind: SecretProviderClass"}, 0))
        document = made(payload={"kind": "List", "items": [POD]})
        document["payload"]["items"][0]["metadata"]["annotations"] = {"a": '{"kind": "Secret"}'}
        document["sha256"] = rec.digest(document["payload"])
        self.assertEqual(rec.problems(document), ["unscrubbed"])  # a hand edit that smuggles one in does not validate

    def test_the_display_filter_keeps_a_safe_set_and_replaces_everything_else(self):
        for text in ("Standard_D4s_v6", "a b.c:d/e,f(g)[h]=i-j"):
            self.assertEqual(rec.sanitise(text), text)
        for char in ("\x1b", "\x00", "\n", "\r", "\t", "$", "`", ";", '"', "|", "&", "<", ">", "\\", "{", "}", "*", "!", "~", "#", "\x7f", "\u00e9", "\u202e"):
            self.assertEqual(rec.sanitise(f"a{char}b"), "a?b", repr(char))
        self.assertEqual(rec.sanitise("x" * 500), "x" * 120)
        self.assertEqual(rec.sanitise("x" * 500, 40), "x" * 40)
        self.assertEqual(rec.sanitise(None), "None")

    def test_scrubbing_does_not_change_its_input(self):
        original = copy.deepcopy(POD)
        rec.scrub(POD)
        self.assertEqual(POD, original)


class Validation(unittest.TestCase):
    def test_every_hand_edit_is_noticed(self):
        base = made()
        edits = {
            "format": lambda d: d.update(format="x"),
            "keys": lambda d: d.update(extra=1),
            "step": lambda d: d.update(step="Z9"),
            "kind": lambda d: d.update(kind="other"),
            "time": lambda d: d.update(recorded_at="yesterday"),
            "tool": lambda d: d.update(tool="/usr/bin/x"),
            "argv": lambda d: d.update(argv=["x", 1]),
            "returncode": lambda d: d.update(returncode="0"),
            "redactions": lambda d: d.update(redactions=-1),
            "sha256": lambda d: d["payload"].update(phase="x"),
        }
        for name, edit in edits.items():
            document = copy.deepcopy(base)
            edit(document)
            with self.subTest(name):
                self.assertTrue(rec.problems(document), name)
        self.assertEqual(rec.problems("not a dict"), ["document"])


class Writing(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_a_recording_is_written_once_with_mode_0600_and_validates_from_disk(self):
        name = rec.write(made(), self.directory.name)
        path = Path(self.directory.name, name)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(rec.problems(json.loads(path.read_text())), [])
        with self.assertRaises(FileExistsError):
            rec.write(made(), self.directory.name)

    def test_it_does_not_follow_a_link_that_is_in_the_way(self):
        document = made()
        target = Path(self.directory.name, "target")
        target.write_text("keep")
        link = Path(self.directory.name, f"{document['step']}-{document['kind']}-{document['sha256'][:10]}.json")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            rec.write(document, self.directory.name)
        self.assertEqual(target.read_text(), "keep")

    def run_main(self, *argv):
        lines = []
        code = rec.main(list(argv), emit=lines.append)
        return code, lines

    def test_import_writes_a_scrubbed_recording_and_prints_no_payload(self):
        source = Path(self.directory.name, "in.json")
        source.write_text(json.dumps(POD))
        out = Path(self.directory.name, "out")
        out.mkdir()
        code, lines = self.run_main("import", "--step", "D5", "--kind", "pod", "--out", str(out), str(source))
        self.assertEqual(code, 0, lines)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^recorded D5 pod: \d+ redactions, file D5-pod-[0-9a-f]{10}\.json$")
        (written,) = out.iterdir()
        self.assertNotIn("sk-ant", written.read_text())
        code, lines = self.run_main("validate", str(written))
        self.assertEqual((code, lines), (0, ["ok recording 1: D5 pod", "recordings: 1 ok, 0 problems"]))

    def test_import_refuses_a_secret_a_bad_json_and_an_unreadable_file(self):
        out = Path(self.directory.name, "out")
        out.mkdir()
        secret = Path(self.directory.name, "secret.json")
        secret.write_text(json.dumps({"kind": "Secret", "data": {"a": "Yg=="}}))
        bad = Path(self.directory.name, "bad.json")
        bad.write_text("{not json")
        for source, expected in ((secret, "secret-object"), (bad, "json")):
            code, lines = self.run_main("import", "--step", "D5", "--kind", "pod", "--out", str(out), str(source))
            self.assertEqual((code, lines), (1, [f"FAIL not recorded: {expected}"]))
        code, lines = self.run_main("import", "--step", "D5", "--kind", "pod", "--out", str(out), str(Path(self.directory.name, "missing")))
        self.assertEqual(code, 2)
        self.assertEqual(list(out.iterdir()), [])

    def test_import_needs_a_step_and_an_existing_directory(self):
        code, _ = self.run_main("import", "--step", "X1", "--kind", "pod", "--out", self.directory.name, "f")
        self.assertEqual(code, 2)
        code, _ = self.run_main("import", "--step", "D5", "--kind", "pod", "--out", str(Path(self.directory.name, "no")), "f")
        self.assertEqual(code, 2)

    def test_validate_fails_for_a_changed_file_and_prints_no_payload(self):
        name = rec.write(made(), self.directory.name)
        path = Path(self.directory.name, name)
        document = json.loads(path.read_text())
        document["payload"]["status"]["phase"] = "Running"
        path.write_text(json.dumps(document))
        garbage = Path(self.directory.name, "garbage")
        garbage.write_text("x")
        code, lines = self.run_main("validate", str(path), str(garbage))
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["FAIL recording 1: sha256", "FAIL recording 2: unreadable", "recordings: 0 ok, 2 problems"])

    def test_kinds_lists_the_vocabulary(self):
        code, lines = self.run_main("kinds")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), len(rec.KINDS))


class Source(unittest.TestCase):
    def test_the_tool_has_no_shell_no_network_and_no_subprocess(self):
        from tests.live_tools import calls, imported_modules

        self.assertFalse(imported_modules("live_recording") & {"subprocess", "socket", "ssl", "http", "urllib", "requests", "ftplib"})
        self.assertFalse(calls("live_recording") & {"os.system", "os.popen", "eval", "exec", "__import__"})


if __name__ == "__main__":
    unittest.main()
