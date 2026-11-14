"""The pod worker wrapper, part B (src/factory/podworker.py): run the agent, stream, heartbeat, delta, footprint, SIGTERM.

Slice 10b of the pod backend (docs/pod-backend.md). CONTRACT-TESTED: the wrapper runs against the in-process relay of
slice 8 (a real `RelayServer` on 127.0.0.1) or a scripted HTTP server, with real temporary directories and REAL CHILD
PROCESSES (a small Python script stands in for `claude`: it reads the prompt on its standard input, writes stream-json
lines and files, and misbehaves on request). No container ran, no cluster was consulted, and the worker identity is the
test's own (the wrapper's `user=`, `group=` and `extra_groups=` are checked on a recording stand-in for `Popen`, because
an unprivileged test cannot switch user). Every wait is on an event or a bounded join; the one rendezvous with the child
is a FIFO, not a sleep.

The text POISON stands for what must never reach a log: the agent writes it to its output and the environment carries it
into the wrapper, and every run is checked (by `assert_fixed` of part A's tests) for appearing on NEITHER of the wrapper's
streams and for only whitelisted lines.
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from factory import agent, bundle, permissions, podreport, podworker, relay
from factory import podcontract as pc
from factory import sessionrecord as sr
from tests import test_podworker as base
from tests.test_podworker import POISON, WORKTREE, Reply, ScriptedRelay, make_bundle

KEY = "sk-ant-api03-" + "k" * 40
PROMPT = b"Please fix the thing.\n"


def b64(data):
    return base64.b64encode(data).decode("ascii")


CLAUDE = """
import json, os, sys, time
prompt = sys.stdin.buffer.read()
print(json.dumps({"type": "system", "prompt_sha": __import__("hashlib").sha256(prompt).hexdigest()}), flush=True)
sys.stderr.write("an error line\\n")
sys.stderr.flush()
with open("README", "w") as handle:
    handle.write("changed by the agent\\n")
print(json.dumps({"type": "result", "subtype": "success", "result": "done"}), flush=True)
"""


class RecordingPopen:
    """A stand-in for `subprocess.Popen` that records every call and starts the real process without the identity keywords."""

    def __init__(self):
        self.calls = []
        self.processes = []

    def __call__(self, command, **keywords):
        self.calls.append((list(command), dict(keywords)))
        real = {key: value for key, value in keywords.items() if key not in ("user", "group", "extra_groups")}
        process = subprocess.Popen(command, **real)
        self.processes.append(process)
        return process


class RunBase(base.Base):
    """A real relay and a session whose bundle carries a `run` section for a fake agent."""

    def setUp(self):
        super().setUp()
        self.start_relay()
        self.script = self.base / "claude.py"
        self.tmp = self.base / "tmp"
        self.tmp.mkdir()
        self.popen = RecordingPopen()
        self.root = os.path.realpath(self.pod)
        self.home = f"{self.root}/home"
        self.prompt_path = f"{self.root}/run/job.prompt.txt"

    # -- the pieces ----------------------------------------------------------------------------------------------

    def claude(self, body=CLAUDE):
        self.script.write_text(textwrap.dedent(body))

    def run_section(self, **changes):
        run = {
            "argv": [sys.executable, os.fspath(self.script)],
            "env": {
                "HOME": self.home,
                "CLAUDE_CONFIG_DIR": f"{self.home}/.claude",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            "cwd": f"{self.root}/{WORKTREE}",
            "prompt": self.prompt_path,
            "timeout": 30,
            "memory_limit_mb": None,
        }
        run.update(changes)
        return run

    def manifest(self, run=None, **changes):
        manifest = {
            "dirs": [self.home, f"{self.home}/.claude"],
            "files": [
                {"path": self.prompt_path, "data": b64(PROMPT), "mode": 0o600},
                {"path": f"{self.root}/run/job.settings.json", "data": b64(b"{}\n"), "mode": 0o640},
            ],
            "run": self.run_section() if run is None else run,
        }
        manifest.update(changes)
        return manifest

    def session(self, manifest=None, tree=None, bound=True):
        data = make_bundle(self.manifest() if manifest is None else manifest, tree=tree)
        return self.make_session(bound=bound, data=data)

    def write_key(self, text=KEY, mode=0o400):
        path = self.secrets / "anthropic-api-key"
        path.unlink(missing_ok=True)
        path.write_text(text)
        os.chmod(path, mode)

    def tuning(self, **changes):
        values = {
            "popen": self.popen,
            "identity_kwargs": lambda identity: {"user": identity[0], "group": identity[1], "extra_groups": [], "umask": 0o002},
            "wrap": False,
            "tmp_dir": os.fspath(self.tmp),
            "post_waits": (0, 0),
            "result_waits": (0, 0),
            "stop_grace": 1.0,
            "drain": 5.0,
            "flush": 20.0,
            "terminated_drain": 2.0,
            "terminated_flush": 8.0,
            "signals": False,
        }
        values.update(changes)
        return podworker.Tuning(**values)

    def go(self, session, token, tuning=None, key=True, **kwargs):
        if key:
            self.write_key()
        kwargs.setdefault("tuning", self.tuning() if tuning is None else tuning)
        return self.run_worker(session, token, runner=None, **kwargs)

    def out(self, session, name):
        return (self.spool / session / "out" / name).read_bytes()

    def report(self, session):
        return podreport.loads(self.out(session, "report.json"))

    def spawned(self):
        return len(self.popen.calls)

    def agent_stderr_lines(self, session):
        return self.out(session, "stderr").decode("utf-8").splitlines()


class Success(RunBase):
    def test_a_session_runs_the_agent_streams_its_output_and_posts_the_delta_and_the_report(self):
        self.claude()
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(
            result.out.splitlines(),
            ["podworker: started", "podworker: preflight ok", "podworker: bundle fetched", "podworker: exit 0", "podworker: result posted"],
        )
        self.assertEqual(result.err, "")
        self.assertEqual(self.state(session)["state"], "finished")
        transcript = self.out(session, "transcript").decode("utf-8").splitlines()
        self.assertEqual(json.loads(transcript[0])["prompt_sha"], hashlib.sha256(PROMPT).hexdigest())
        self.assertEqual(json.loads(transcript[-1]), {"type": "result", "subtype": "success", "result": "done"})
        self.assertEqual(self.agent_stderr_lines(session), ["an error line"])
        report = self.report(session)
        self.assertEqual((report["outcome"], report["exit_code"]), ("exited", 0))
        self.assertEqual(report["streamed"], {"transcript": len(self.out(session, "transcript")), "stderr": len(self.out(session, "stderr"))})
        self.assertEqual(report["dropped"], {"transcript": False, "stderr": False})
        self.assertEqual(report["delta"]["status"], "ok")
        self.assertEqual(report["delta"]["bytes"], len(self.out(session, "delta")))

    def test_the_delta_applies_to_the_supervisors_tree_with_the_numbers_the_report_declares(self):
        self.claude(
            CLAUDE.replace(
                'print(json.dumps({"type": "result"',
                'os.unlink("src/a.py"); os.makedirs("new/dir"); open("new/dir/f.txt", "w").write("new\\n"); os.symlink("README", "alias")\n'
                'os.chmod("README", 0o755)\nprint(json.dumps({"type": "result"',
            )
        )
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        report = self.report(session)
        supervisor = self.base / "supervisor"
        for path, data in {"README": b"hello\n", "src/a.py": b"print(1)\n"}.items():
            (supervisor / path).parent.mkdir(parents=True, exist_ok=True)
            (supervisor / path).write_bytes(data)
        applied = bundle.apply_delta(
            io.BytesIO(self.out(session, "delta")),
            supervisor,
            write_scope="cwd",
            declared_records=report["delta"]["records"],
            declared_bytes=report["delta"]["bytes"],
        )
        self.assertEqual((applied.files, applied.symlinks, applied.deleted), (2, 1, 1))
        self.assertEqual((supervisor / "README").read_text(), "changed by the agent\n")
        self.assertEqual(stat.S_IMODE(os.lstat(supervisor / "README").st_mode), 0o755)
        self.assertFalse((supervisor / "src" / "a.py").exists())
        self.assertEqual((supervisor / "new" / "dir" / "f.txt").read_text(), "new\n")
        self.assertEqual(os.readlink(supervisor / "alias"), "README")

    def test_an_agent_that_changes_nothing_returns_an_empty_delta_that_is_still_a_delta(self):
        self.claude("import sys\nsys.stdin.buffer.read()\n")
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.out(session, "delta"), bundle.HEADER)
        report = self.report(session)
        self.assertEqual(report["delta"], {"status": "ok", "records": 0, "bytes": len(bundle.HEADER)})

    def test_the_exit_status_of_the_agent_is_reported_and_is_not_the_wrappers_exit(self):
        self.claude("import sys\nsys.stdin.buffer.read()\nsys.exit(3)\n")
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertIn("podworker: exit 3", result.out.splitlines())
        self.assertEqual(self.report(session)["exit_code"], 3)

    def test_an_agent_ended_by_a_signal_is_reported_as_the_negative_signal_and_as_128_plus_it_on_the_line(self):
        self.claude("import os, signal, sys\nsys.stdin.buffer.read()\nos.kill(os.getpid(), signal.SIGKILL)\n")
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertIn("podworker: exit 137", result.out.splitlines())
        self.assertEqual(self.report(session)["exit_code"], -9)

    def test_the_footprint_in_the_report_is_the_memory_guards_record_or_null(self):
        self.claude("import sys, time\nsys.stdin.buffer.read()\ntime.sleep(0.3)\n")
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        footprint = self.report(session)["footprint"]
        if footprint is not None:
            self.assertEqual(set(footprint), {"peak_rss_mb", "cpu_seconds", "samples", "limit_mb"})
            self.assertGreaterEqual(footprint["samples"], 1)
            self.assertIsNone(footprint["limit_mb"])

    def test_a_large_prompt_does_not_block_the_wrapper_and_an_agent_that_never_reads_it_is_not_an_error(self):
        big = os.urandom(120 * 1024)  # more than a pipe holds, less than the manifest allows
        self.claude("import hashlib, sys\nsys.stderr.write(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())\n")
        manifest = self.manifest()
        manifest["files"][0]["data"] = b64(big)
        session, token = self.session(manifest)
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.agent_stderr_lines(session), [hashlib.sha256(big).hexdigest()])
        self.claude("print('never reads')\n")
        session, token = self.session(manifest)
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.report(session)["outcome"], "exited")

    def test_a_holder_of_the_agents_input_outside_its_group_cannot_hang_the_wrapper_after_the_result_is_posted(self):
        big = os.urandom(150 * 1024)  # more than a pipe holds: the prompt thread is blocked for lack of room
        self.claude(
            """
            import subprocess, sys
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )  # inherits the agent's input, and is not in the agent's group: nobody will ever read it, nobody kills it
            sys.stderr.write("child=%d\\n" % child.pid)
            """
        )
        manifest = self.manifest()
        manifest["files"][0]["data"] = b64(big)
        session, token = self.session(manifest)
        self.write_key()
        done = []

        def run():
            done.append(self.run_worker(session, token, runner=None, tuning=self.tuning(drain=0.5)))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(30)
        pid = None
        with contextlib.suppress(OSError, ValueError, IndexError):
            pid = int(self.agent_stderr_lines(session)[0].split("=")[1])
        if pid is not None:
            self.addCleanup(self.kill_quietly, pid)
        self.assertFalse(thread.is_alive(), "the wrapper hung in the close of the agent's input")
        self.assert_fixed(done[0], code=0)
        self.assertEqual(self.report(session)["outcome"], "exited")

    @staticmethod
    def kill_quietly(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def test_the_prompt_is_written_to_the_raw_descriptor_and_never_through_the_buffered_writer(self):
        import ast

        tree = ast.parse(Path(podworker.__file__).read_text(encoding="utf-8"))
        feed = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_feed")
        calls = [node.func for node in ast.walk(feed) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        writes = [call for call in calls if call.attr in ("write", "writelines", "flush")]
        # ``stdin.write`` takes the lock of the buffered writer that ``close`` needs; only ``os.write`` may be used
        self.assertTrue(writes)
        for call in writes:
            self.assertTrue(isinstance(call.value, ast.Name) and call.value.id == "os", ast.dump(call))
        self.assertTrue({"fileno", "set_blocking"} <= {call.attr for call in calls})

    def test_the_real_capability_dropping_wrapper_runs_the_agent(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token, tuning=self.tuning(wrap=True)), code=0)
        self.assertEqual(self.popen.calls[0][0][:4], [sys.executable, "-I", "-S", os.fspath(Path(podworker.__file__).with_name("execnocaps.py"))])
        self.assertEqual(self.popen.calls[0][0][4], "--")
        self.assertEqual(self.report(session)["exit_code"], 0)
        self.assertIn(b'"result"', self.out(session, "transcript"))

    def test_without_the_wrapper_the_command_is_the_manifests_argv_exactly(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.popen.calls[0][0], [sys.executable, os.fspath(self.script)])


class Isolation(RunBase):
    """What the agent gets: the worker identity, its own session, pipes, a minimal environment, and no secret but the key."""

    def dump(self):
        self.claude(
            """
            import json, os, sys
            sys.stdin.buffer.read()
            sys.stderr.write(json.dumps({"env": dict(os.environ), "argv": sys.argv, "cwd": os.getcwd(), "umask": os.umask(0o022)}))
            """
        )

    def test_the_agent_starts_as_the_worker_in_its_own_group_with_pipes_and_no_extra_groups(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        command, keywords = self.popen.calls[0]
        self.assertEqual(keywords["user"], base.IDENTITY[0])
        self.assertEqual(keywords["group"], base.IDENTITY[1])
        self.assertEqual(keywords["extra_groups"], [])
        self.assertEqual(keywords["umask"], permissions.WORKER_UMASK)
        self.assertIs(keywords["start_new_session"], True)
        self.assertIs(keywords["close_fds"], True)
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(keywords[name], subprocess.PIPE, name)
        self.assertEqual(keywords["cwd"], f"{self.root}/{WORKTREE}")

    def test_the_default_identity_keywords_are_exactly_the_backends(self):
        self.assertEqual(
            podworker.identity_kwargs((10002, 10010)),
            {"user": 10002, "group": 10010, "extra_groups": [], "umask": permissions.WORKER_UMASK},
        )

    def test_the_agents_environment_is_the_allow_list_the_manifests_names_and_the_key_and_nothing_else(self):
        self.dump()
        session, token = self.session()
        environ = self.environment(session)
        environ.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GH_TOKEN": POISON, "AZURE_CLIENT_ID": POISON, "FOO": POISON, "ANTHROPIC_API_KEY": POISON})
        self.assert_fixed(self.go(session, token, env=environ), code=0)
        seen = json.loads(self.out(session, "stderr"))
        self.assertEqual(
            seen["env"],
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "HOME": self.home,
                "CLAUDE_CONFIG_DIR": f"{self.home}/.claude",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "ANTHROPIC_API_KEY": KEY,
            },
        )
        self.assertEqual(seen["cwd"], f"{self.root}/{WORKTREE}")
        self.assertEqual(seen["umask"], permissions.WORKER_UMASK)

    def test_no_secret_reaches_the_agents_argument_list_or_the_environment_besides_the_key_in_its_own_variable(self):
        self.dump()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        seen = self.out(session, "stderr").decode("utf-8")
        self.assertNotIn(token, seen)
        self.assertNotIn(KEY, json.dumps(json.loads(seen)["argv"]))
        self.assertEqual([name for name, value in json.loads(seen)["env"].items() if KEY in value], ["ANTHROPIC_API_KEY"])
        for name in pc.ENV_POD:
            self.assertNotIn(name, json.loads(seen)["env"], name)
        for call in self.popen.calls:
            self.assertNotIn(token, json.dumps(call[0]))
            self.assertNotIn(KEY, json.dumps(call[0]))
            self.assertNotIn(token, json.dumps(dict(call[1]["env"])))

    def test_the_child_environment_builder_copies_only_the_inherited_names(self):
        spec = podworker.RunSpec(("x",), {"HOME": "/h", "CLAUDE_CONFIG_DIR": "/h/.claude"}, "/w", b"", 1.0, None)
        environ = {name: name.lower() for name in podworker.CHILD_ENV_KEYS}
        environ.update({"FACTORY_RELAY_URL": POISON, "FACTORY_SESSION_ID": POISON, "KUBERNETES_SERVICE_HOST": POISON})
        env = podworker.child_env(environ, spec, KEY)
        self.assertEqual(set(env), {*podworker.CHILD_ENV_KEYS, "HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY"})
        self.assertNotIn(POISON, " ".join(env.values()))

    def test_the_names_and_the_umask_agree_with_the_in_process_backend(self):
        self.assertEqual(podworker.CHILD_ENV_KEYS, agent.CHILD_ENV_KEYS)
        self.assertEqual(podworker.WORKER_ENV_NAMES, tuple(agent.worker_env("/h", "/c")))
        self.assertEqual(podworker.WORKER_UMASK, permissions.WORKER_UMASK)

    def test_the_wrappers_streams_never_carry_what_the_agent_writes(self):
        self.claude(
            f"""
            import os, sys
            sys.stdin.buffer.read()
            sys.stdout.write({POISON!r} + "\\n"); sys.stdout.flush()
            sys.stderr.write({POISON!r} + "\\n"); sys.stderr.flush()
            os.write(1, b"{POISON}-fd1\\n"); os.write(2, b"{POISON}-fd2\\n")
            """
        )
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertIn(POISON, self.out(session, "transcript").decode("utf-8"))
        self.assertIn(POISON, self.out(session, "stderr").decode("utf-8"))

    def test_the_agent_gets_the_prompt_from_the_manifest_and_the_worker_never_has_the_prompt_file(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        prompt = Path(self.prompt_path)
        self.assertEqual(stat.S_IMODE(prompt.stat().st_mode), 0o600)
        self.assertEqual([entry for entry in self.chowns if entry[0] == self.prompt_path], [])


class RunSpecs(RunBase):
    """Hostile or broken `run` sections: the `run` stage, and nothing is started and nothing is streamed."""

    def assert_refused(self, manifest, stage="run"):
        session, token = self.session(manifest)
        before = self.spawned()
        result = self.go(session, token)
        self.assert_fixed(result, stage=stage)
        self.assertEqual(self.spawned(), before)
        self.assertEqual(self.state(session)["state"], "fetched")
        self.assertFalse((self.spool / session / "out" / "transcript").exists())
        return result

    def test_a_manifest_without_a_run_section_fails_closed(self):
        manifest = self.manifest()
        del manifest["run"]
        result = self.assert_refused(manifest)
        self.assertEqual(result.out.splitlines()[-1], "podworker: bundle fetched")

    def test_every_malformed_run_section_is_refused_before_anything_starts(self):
        run = self.run_section()
        bad = {
            "not an object": "run",
            "a list": [],
            "no argv": {key: value for key, value in run.items() if key != "argv"},
            "an extra key": {**run, "user": 0},
            "empty argv": {**run, "argv": []},
            "an empty program": {**run, "argv": [""]},
            "argv not strings": {**run, "argv": ["x", 1]},
            "argv with a NUL": {**run, "argv": ["x", "a\0b"]},
            "argv too long": {**run, "argv": ["x"] * (podworker.MAX_ARGV + 1)},
            "an argument too long": {**run, "argv": ["x" * (podworker.MAX_ARG_CHARS + 1)]},
            "a lone surrogate": {**run, "argv": ["x\udc80"]},
            "env a list": {**run, "env": []},
            "env without HOME": {**run, "env": {key: value for key, value in run["env"].items() if key != "HOME"}},
            "env with the key": {**run, "env": {**run["env"], "ANTHROPIC_API_KEY": "x"}},
            "env with PATH": {**run, "env": {**run["env"], "PATH": "/tmp/evil"}},
            "env with LD_PRELOAD": {**run, "env": {**run["env"], "LD_PRELOAD": "/tmp/evil.so"}},
            "env value not text": {**run, "env": {**run["env"], "DISABLE_TELEMETRY": 1}},
            "env value too long": {**run, "env": {**run["env"], "DISABLE_TELEMETRY": "x" * (podworker.MAX_ENV_VALUE_CHARS + 1)}},
            "HOME not created": {**run, "env": {**run["env"], "HOME": f"{self.root}/elsewhere"}},
            "config not created": {**run, "env": {**run["env"], "CLAUDE_CONFIG_DIR": f"{self.root}/elsewhere"}},
            "cwd elsewhere": {**run, "cwd": f"{self.root}/work"},
            "cwd below the worktree": {**run, "cwd": f"{self.root}/{WORKTREE}/src"},
            "cwd relative": {**run, "cwd": WORKTREE},
            "prompt not in the manifest": {**run, "prompt": f"{self.root}/run/other.txt"},
            "prompt is a 0640 file": {**run, "prompt": f"{self.root}/run/job.settings.json"},
            "prompt not text": {**run, "prompt": 7},
            "timeout zero": {**run, "timeout": 0},
            "timeout negative": {**run, "timeout": -1},
            "timeout a bool": {**run, "timeout": True},
            "timeout text": {**run, "timeout": "30"},
            "timeout nan": {**run, "timeout": float("nan")},
            "timeout beyond the clamp": {**run, "timeout": pc.DEADLINE_CLAMP_SECONDS + 1},
            "memory limit zero": {**run, "memory_limit_mb": 0},
            "memory limit a bool": {**run, "memory_limit_mb": True},
            "memory limit text": {**run, "memory_limit_mb": "4096"},
            "memory limit huge": {**run, "memory_limit_mb": 1 << 40},
        }
        for label, section in bad.items():
            with self.subTest(label):
                self.assert_refused(self.manifest(run=section))

    def test_the_good_section_the_cases_above_start_from_runs(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.spawned(), 1)

    def test_a_prompt_that_two_manifest_files_claim_is_refused(self):
        manifest = self.manifest()
        manifest["files"].append({"path": self.prompt_path, "data": b64(b"x"), "mode": 0o600})
        self.assert_refused(manifest, stage="manifest")  # part A refuses a path claimed twice, before part B starts

    def test_a_token_deadline_with_no_time_left_is_refused_before_anything_starts(self):
        self.claude()
        session, token = self.session()
        env = self.environment(session, FACTORY_SESSION_DEADLINE=str(int(base.time.time()) + 30))
        result = self.go(session, token, env=env)  # 30 s left, less the 60 s reserve: nothing is left
        self.assert_fixed(result, stage="run")
        self.assertEqual(self.spawned(), 0)


class KeyFile(RunBase):
    def test_a_missing_or_unsafe_or_malformed_key_file_is_the_key_stage_and_nothing_starts(self):
        self.claude()
        for label, setup in {
            "missing": lambda: (self.secrets / "anthropic-api-key").unlink(missing_ok=True),
            "group readable": lambda: self.write_key(mode=0o440),
            "world readable": lambda: self.write_key(mode=0o404),
            "empty": lambda: self.write_key(text=""),
            "too short": lambda: self.write_key(text="short"),
            "has a space": lambda: self.write_key(text=f"{KEY} {POISON}"),
            "has a newline inside": lambda: self.write_key(text=f"{KEY}\n{POISON}"),
            "non ascii": lambda: self.write_key(text=f"{KEY}é"),
        }.items():
            with self.subTest(label):
                session, token = self.session()
                setup()
                result = self.go(session, token, key=False)
                self.assert_fixed(result, stage="key")
                self.assertEqual(self.spawned(), 0)
        session, token = self.session()
        (self.secrets / "anthropic-api-key").unlink(missing_ok=True)
        (self.secrets / "anthropic-api-key").mkdir()
        self.assert_fixed(self.go(session, token, key=False), stage="key")

    def test_one_trailing_newline_is_dropped_and_the_key_is_the_agents(self):
        self.claude(CLAUDE.replace('print(json.dumps({"type": "result"', 'sys.stderr.write(os.environ["ANTHROPIC_API_KEY"]); print(json.dumps({"type": "result"'))
        session, token = self.session()
        self.write_key(text=f"{KEY}\n")
        self.assert_fixed(self.go(session, token, key=False), code=0)
        self.assertIn(KEY, self.out(session, "stderr").decode("utf-8"))

    def test_the_key_is_never_in_the_report_the_delta_or_the_wrappers_streams(self):
        self.claude()
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertNotIn(KEY, result.text)
        for name in ("report.json", "delta"):
            self.assertNotIn(KEY.encode(), self.out(session, name))
        self.assertNotIn(KEY.encode(), json.dumps(self.report(session)).encode())


class Spawning(RunBase):
    def test_an_agent_that_cannot_be_started_is_the_spawn_stage_with_no_text(self):
        self.claude()
        for error in (OSError(2, POISON, POISON), ValueError(POISON), subprocess.SubprocessError(POISON), PermissionError(POISON)):
            with self.subTest(error=type(error).__name__):

                def failing(command, error=error, **keywords):
                    raise error

                session, token = self.session()
                result = self.go(session, token, tuning=self.tuning(popen=failing))
                self.assert_fixed(result, stage="spawn")
                self.assertEqual(self.state(session)["state"], "fetched")

    def test_a_program_that_does_not_exist_is_a_spawn_failure_with_the_real_popen(self):
        self.claude()
        manifest = self.manifest(run=self.run_section(argv=[f"{self.root}/missing/{POISON}"]))
        session, token = self.session(manifest)
        result = self.go(session, token, tuning=self.tuning(popen=subprocess.Popen, identity_kwargs=lambda identity: {}))
        self.assert_fixed(result, stage="spawn")

    def test_an_exception_in_the_run_is_one_fixed_line_of_the_run_stage(self):
        self.claude()
        session, token = self.session()
        with mock.patch.object(podworker.Session, "_supervise", side_effect=RuntimeError(POISON)):
            result = self.go(session, token)
        self.assert_fixed(result, stage="run")
        self.assertEqual(self.spawned(), 1)

    def test_an_agent_is_never_left_running_when_the_run_fails_after_it_started(self):
        self.claude("import sys, time\nsys.stdin.buffer.read()\ntime.sleep(60)\n")
        manifest = self.manifest()
        session, token = self.session(manifest)
        with mock.patch.object(podworker.Session, "_supervise", side_effect=RuntimeError(POISON)):
            result = self.go(session, token)
        self.assert_fixed(result, stage="run")
        self.assertEqual(self.popen.processes[0].wait(10), -signal.SIGKILL)  # the group was killed, not left to sleep


class Streaming(RunBase):
    def test_a_large_transcript_arrives_complete_and_in_order_in_more_than_one_post(self):
        lines = 40_000
        self.claude(
            f"""
            import json, sys
            sys.stdin.buffer.read()
            for number in range({lines}):
                print(json.dumps({{"type": "assistant", "n": number, "pad": "x" * 60}}), flush=(number % 5000 == 0))
            sys.stdout.flush()
            """
        )
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        got = self.out(session, "transcript").decode("utf-8").splitlines()
        self.assertEqual([json.loads(line)["n"] for line in got], list(range(lines)))
        self.assertEqual(self.report(session)["streamed"]["transcript"], len(self.out(session, "transcript")))

    def test_the_two_streams_are_independent_and_both_complete(self):
        self.claude(
            """
            import sys
            sys.stdin.buffer.read()
            for number in range(3000):
                sys.stdout.write("out %d\\n" % number)
                sys.stderr.write("err %d\\n" % number)
            """
        )
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        self.assertEqual(self.out(session, "transcript").decode().splitlines(), [f"out {n}" for n in range(3000)])
        self.assertEqual(self.out(session, "stderr").decode().splitlines(), [f"err {n}" for n in range(3000)])

    def test_the_credentials_travel_in_headers_on_every_request_and_the_body_never_carries_them(self):
        self.claude()
        script = {
            ("POST", "/v1/preflight"): [Reply(200)],
            ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
            ("POST", "/v1/stream/transcript"): [Reply(200)],
            ("POST", "/v1/stream/stderr"): [Reply(200)],
            ("POST", "/v1/heartbeat"): [Reply(200)],
            ("POST", "/v1/result"): [Reply(200)],
        }
        scripted = ScriptedRelay(self, script)
        session, token = pc.new_session_id(), sr.new_token()
        self.assert_fixed(self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port))), code=0)
        paths = {path for _, path, _, _ in scripted.log}
        self.assertEqual(paths, set(path for _, path in script))
        for _method, path, headers, body in scripted.log:
            with self.subTest(path=path):
                self.assertEqual(headers["Authorization"], f"Bearer {token}")
                self.assertEqual(headers[podworker.HEADER_POD_NAME], pc.pod_name(session))
                self.assertNotIn(token.encode(), body)
                self.assertNotIn(KEY.encode(), body)
                self.assertNotIn(token, path)
        stream = scripted.requests("POST", "/v1/stream/transcript")
        self.assertEqual(stream[0][2]["Content-Type"], "application/octet-stream")

    def test_a_dead_relay_never_blocks_the_agent_its_output_is_dropped_and_said_so(self):
        self.claude(
            """
            import sys
            sys.stdin.buffer.read()
            for number in range(20000):
                sys.stdout.write("line %d %s\\n" % (number, "x" * 100))
            """
        )
        script = {
            ("POST", "/v1/preflight"): [Reply(200)],
            ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
            ("POST", "/v1/stream/transcript"): [Reply(503)],
            ("POST", "/v1/stream/stderr"): [Reply(503)],
            ("POST", "/v1/heartbeat"): [Reply(200)],
            ("POST", "/v1/result"): [Reply(200)],
        }
        scripted = ScriptedRelay(self, script)
        session, token = pc.new_session_id(), sr.new_token()
        result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)))
        self.assert_fixed(result, code=0)
        body = scripted.requests("POST", "/v1/result")[0][3]
        size = int.from_bytes(body[:4], "big")
        document = podreport.loads(body[4 : 4 + size])
        self.assertEqual(document["dropped"], {"transcript": True, "stderr": False})
        self.assertEqual(document["streamed"]["transcript"], 0)
        self.assertEqual(len(scripted.requests("POST", "/v1/stream/transcript")), 3)  # tried three times, then given up for good

    def test_the_result_body_is_the_relays_format_a_length_the_report_then_the_delta(self):
        self.claude()
        scripted = ScriptedRelay(
            self,
            {
                ("POST", "/v1/preflight"): [Reply(200)],
                ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
                ("POST", "/v1/stream/transcript"): [Reply(200)],
                ("POST", "/v1/stream/stderr"): [Reply(200)],
                ("POST", "/v1/heartbeat"): [Reply(200)],
                ("POST", "/v1/result"): [Reply(200)],
            },
        )
        session, token = pc.new_session_id(), sr.new_token()
        self.assert_fixed(self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port))), code=0)
        (_, _, headers, body), = scripted.requests("POST", "/v1/result")
        self.assertEqual(headers["Content-Length"], str(len(body)))
        size = int.from_bytes(body[:4], "big")
        report = podreport.loads(body[4 : 4 + size])
        delta = body[4 + size :]
        self.assertEqual(report["delta"]["bytes"], len(delta))
        records = list(bundle.Reader(io.BytesIO(delta), bundle.DELTA))
        self.assertEqual([(record.kind, record.path) for record in records], [(bundle.FILE, "README")])
        self.assertEqual(report["delta"]["records"], 1)


class PumpUnits(unittest.TestCase):
    """The pump on a real pipe with a scripted `post`."""

    def setUp(self):
        self.read, self.write = os.pipe()
        self.addCleanup(self.close_quietly, self.read)
        self.gone = []
        self.posts = []

    @staticmethod
    def close_quietly(fd):
        with contextlib.suppress(OSError):
            os.close(fd)

    def pump(self, post, waits=(0, 0)):
        pump = podworker.Pump("transcript", self.read, post, lambda: self.gone.append(1), waits)
        pump.start()
        return pump

    def test_the_bytes_arrive_in_order_in_posts_of_at_most_the_send_size(self):
        def post(name, data):
            self.posts.append(data)
            return 200

        pump = self.pump(post)
        expected = os.urandom(2 * podworker.STREAM_SEND_BYTES + 12345)
        for start in range(0, len(expected), 50_000):
            os.write(self.write, expected[start : start + 50_000])
        os.close(self.write)
        pump.finish(5, 5)
        self.assertEqual(b"".join(self.posts), expected)
        self.assertTrue(all(0 < len(data) <= podworker.STREAM_SEND_BYTES for data in self.posts))
        self.assertEqual((pump.sent, pump.dropped), (len(expected), False))

    def test_a_failed_post_is_tried_three_times_then_the_stream_is_over_and_everything_after_is_dropped(self):
        self.third = threading.Event()

        def post(name, data):
            self.posts.append(data)
            if len(self.posts) == 3:
                self.third.set()
            return None

        pump = self.pump(post)
        os.write(self.write, b"first")
        self.assertTrue(self.third.wait(10))
        os.write(self.write, b"second" * 1000)
        os.close(self.write)
        pump.finish(5, 5)
        self.assertEqual(self.posts, [b"first"] * 3)
        self.assertEqual((pump.sent, pump.dropped), (0, True))
        self.assertEqual(self.gone, [])

    def test_an_answer_of_5xx_is_retried_and_a_later_200_recovers_without_loss(self):
        answers = iter([503, 500, 200, 200])

        def post(name, data):
            self.posts.append(data)
            return next(answers)

        pump = self.pump(post)
        os.write(self.write, b"hello ")
        os.close(self.write)
        pump.finish(5, 5)
        self.assertEqual(self.posts, [b"hello "] * 3)
        self.assertEqual((pump.sent, pump.dropped), (6, False))

    def test_a_4xx_answer_ends_the_session_once_and_is_not_retried(self):
        for status in (400, 401, 403, 404, 409, 410, 413):
            with self.subTest(status=status):
                self.setUp()
                self.posts.clear()

                def post(name, data, status=status):
                    self.posts.append(data)
                    return status

                pump = self.pump(post)
                os.write(self.write, b"data")
                os.close(self.write)
                pump.finish(5, 5)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(self.gone, [1])
                self.assertTrue(pump.dropped)
                self.gone.clear()

    def test_a_pipe_that_another_process_keeps_open_is_abandoned_in_bounded_time(self):
        def post(name, data):
            self.posts.append(data)
            return 200

        pump = self.pump(post)
        os.write(self.write, b"before")  # the write end stays open: no end of file will ever come
        done = threading.Event()
        finisher = threading.Thread(target=lambda: (pump.finish(0.3, 5), done.set()), daemon=True)
        finisher.start()
        self.assertTrue(done.wait(10))
        self.assertEqual(b"".join(self.posts), b"before")
        self.assertTrue(pump.reader_done())
        os.close(self.write)

    def test_a_full_buffer_stops_the_reader_so_memory_is_bounded_and_nothing_is_lost_while_the_relay_answers(self):
        release = threading.Event()
        started = threading.Event()

        def post(name, data):
            started.set()
            release.wait(10)
            self.posts.append(data)
            return 200

        with mock.patch.object(podworker, "STREAM_BUFFER_BYTES", 128 * 1024), mock.patch.object(podworker, "STREAM_SEND_BYTES", 64 * 1024):
            pump = self.pump(post)
            expected = os.urandom(1024 * 1024)

            def feed():
                for start in range(0, len(expected), 4096):
                    os.write(self.write, expected[start : start + 4096])

            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            self.assertTrue(started.wait(10))
            # the sender is blocked: the reader fills the buffer and then stops reading, however much more the agent writes
            pause = threading.Event()
            for _ in range(500):
                if pump._held >= 128 * 1024:
                    break
                pause.wait(0.01)
            self.assertGreaterEqual(pump._held, 128 * 1024)
            pause.wait(0.3)
            self.assertLessEqual(pump._held, 128 * 1024 + podworker.STREAM_SLICE_BYTES)
            release.set()
            writer.join(10)
            os.close(self.write)
            pump.finish(10, 10)
        self.assertEqual(b"".join(self.posts), expected)

    def test_the_sender_that_is_stuck_is_abandoned_by_the_flush_time(self):
        hold = threading.Event()
        self.addCleanup(hold.set)

        def post(name, data):
            hold.wait(30)
            return 200

        pump = self.pump(post)
        os.write(self.write, b"data")
        os.close(self.write)
        done = threading.Event()
        threading.Thread(target=lambda: (pump.finish(5, 0.3), done.set()), daemon=True).start()
        self.assertTrue(done.wait(10))
        hold.set()


class SharedDeadlines(unittest.TestCase):
    """Draining and flushing the pumps share ONE deadline each: more pumps never add time."""

    def wedged_pumps(self, count, hold, data=b"data"):
        """``count`` pumps whose pipe never ends and whose post never answers (a holder outside the group, a dead relay)."""
        pumps, writers = [], []
        for _ in range(count):
            read, write = os.pipe()
            self.addCleanup(os.close, read)
            self.addCleanup(os.close, write)
            pump = podworker.Pump("transcript", read, lambda name, data: (hold.wait(30), 200)[1], lambda: None, (0, 0))
            pump.start()
            if data:
                os.write(write, data)  # something buffered, so the sender is inside its post
            pumps.append(pump)
            writers.append(write)
        return pumps

    def test_the_pumps_are_finished_against_one_drain_and_one_flush_deadline_not_one_after_the_other(self):
        hold = threading.Event()
        self.addCleanup(hold.set)
        pumps = self.wedged_pumps(4, hold)
        begun = time.monotonic()
        podworker.finish_pumps(pumps, 0.5, 1.0)
        elapsed = time.monotonic() - begun
        # one after the other this would be 4 times (0.5 + 1.0 + the joins), about 10 s
        self.assertLess(elapsed, 0.5 + 1.0 + 2 * podworker.ABANDON_JOIN_SECONDS + 1.0)
        self.assertGreaterEqual(elapsed, 1.0)  # the flush did wait its time for the stuck senders
        for pump in pumps:
            self.assertTrue(pump.dropped)
            self.assertTrue(pump.reader_done())
        hold.set()

    def test_the_readers_of_all_the_pumps_share_one_drain_deadline(self):
        hold = threading.Event()
        pumps = self.wedged_pumps(4, hold, data=b"")  # nothing to send: only the readers are wedged
        begun = time.monotonic()
        podworker.finish_pumps(pumps, 0.5, 5.0)
        elapsed = time.monotonic() - begun
        self.assertLess(elapsed, 1.4)  # one after the other: four drains of 0.5 s, more than 2 s
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertTrue(all(pump.reader_done() for pump in pumps))

    def test_a_pump_that_finishes_alone_has_the_same_bound(self):
        hold = threading.Event()
        self.addCleanup(hold.set)
        (pump,) = self.wedged_pumps(1, hold)
        begun = time.monotonic()
        pump.finish(0.3, 0.3)
        self.assertLess(time.monotonic() - begun, 0.3 + 0.3 + 2 * podworker.ABANDON_JOIN_SECONDS + 1.0)
        hold.set()

    def test_the_worst_case_after_sigterm_leaves_the_delta_a_real_share_of_the_contracts_grace(self):
        worst = podworker.TERMINATED_WORST_SECONDS
        self.assertEqual(
            worst,
            2 * podworker.STOP_GRACE_SECONDS
            + podworker.TERMINATED_DRAIN_SECONDS
            + podworker.TERMINATED_FLUSH_SECONDS
            + 2 * podworker.ABANDON_JOIN_SECONDS,
        )
        self.assertLessEqual(worst + 5, pc.TERMINATION_GRACE_SECONDS)  # at least 5 s for the delta and the result


class Heartbeats(RunBase):
    def scripted(self, heartbeat, claude_seconds):
        self.claude(f"import sys, time\nsys.stdin.buffer.read()\ntime.sleep({claude_seconds})\n")
        script = {
            ("POST", "/v1/preflight"): [Reply(200)],
            ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
            ("POST", "/v1/stream/transcript"): [Reply(200)],
            ("POST", "/v1/stream/stderr"): [Reply(200)],
            ("POST", "/v1/heartbeat"): heartbeat,
            ("POST", "/v1/result"): [Reply(200)],
        }
        return ScriptedRelay(self, script)

    def test_a_heartbeat_goes_out_at_once_and_then_every_interval_with_the_pods_credentials(self):
        scripted = self.scripted([Reply(200)], 0.6)
        session, token = pc.new_session_id(), sr.new_token()
        result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)), tuning=self.tuning(heartbeat_seconds=0.05))
        self.assert_fixed(result, code=0)
        beats = scripted.requests("POST", "/v1/heartbeat")
        self.assertGreaterEqual(len(beats), 3)
        self.assertEqual(beats[0][2]["Authorization"], f"Bearer {token}")
        self.assertEqual(beats[0][2][podworker.HEADER_POD_UID], base.UID)

    def test_the_default_interval_is_the_contracts_and_no_beat_follows_the_end_of_the_session(self):
        self.assertIsNone(podworker.Tuning().heartbeat_seconds)
        self.claude("import sys\nsys.stdin.buffer.read()\n")
        scripted = self.scripted([Reply(200)], 0)
        session, token = pc.new_session_id(), sr.new_token()
        self.assert_fixed(self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port))), code=0)
        self.assertEqual(len(scripted.requests("POST", "/v1/heartbeat")), 1)  # one at the start; the 30 s interval never passed

    def test_the_heartbeat_goes_on_through_the_delta_read_and_stops_once_the_result_is_posted(self):
        scripted = self.scripted([Reply(200)], 0)
        session, token = pc.new_session_id(), sr.new_token()
        seen = {}
        real = bundle.produce_delta
        pause = threading.Event()

        def slow(*args, **keywords):  # a long delta: the agent is long gone, the relay must keep hearing from the pod
            before = len(scripted.requests("POST", "/v1/heartbeat"))
            for _ in range(500):
                if len(scripted.requests("POST", "/v1/heartbeat")) >= before + 3:
                    break
                pause.wait(0.02)
            seen["beats"] = len(scripted.requests("POST", "/v1/heartbeat")) - before
            seen["results"] = len(scripted.requests("POST", "/v1/result"))
            return real(*args, **keywords)

        with mock.patch.object(bundle, "produce_delta", slow):
            result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)), tuning=self.tuning(heartbeat_seconds=0.05))
        self.assert_fixed(result, code=0)
        self.assertGreaterEqual(seen["beats"], 3)
        self.assertEqual(seen["results"], 0)
        after = len(scripted.requests("POST", "/v1/heartbeat"))
        pause.wait(0.3)
        self.assertEqual(len(scripted.requests("POST", "/v1/heartbeat")), after)  # none follows the posted result

    def test_a_heartbeat_the_relay_refuses_ends_the_agent_and_posts_no_result(self):
        for status in (410, 403, 401, 404):
            with self.subTest(status=status):
                scripted = self.scripted([Reply(status)], 30)
                session, token = pc.new_session_id(), sr.new_token()
                result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)), tuning=self.tuning(heartbeat_seconds=0.05))
                self.assert_fixed(result, stage="gone")
                self.assertEqual(scripted.requests("POST", "/v1/result"), [])
                self.assertEqual(result.out.splitlines()[-1], "podworker: bundle fetched")  # no exit line either: nothing is reported

    def test_a_heartbeat_the_relay_does_not_answer_is_not_the_end_of_the_session(self):
        scripted = self.scripted([Reply(503)], 0.3)
        session, token = pc.new_session_id(), sr.new_token()
        result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)), tuning=self.tuning(heartbeat_seconds=0.05))
        self.assert_fixed(result, code=0)
        self.assertEqual(len(scripted.requests("POST", "/v1/result")), 1)


class RelayEndsTheSession(RunBase):
    def test_a_session_revoked_by_the_supervisor_stops_the_agent_and_posts_nothing(self):
        fifo = self.base / "ready"
        os.mkfifo(fifo)
        self.claude(
            f"""
            import sys, time
            sys.stdin.buffer.read()
            open({os.fspath(fifo)!r}, "w").write("up")
            time.sleep(30)
            """
        )
        session, token = self.session()

        def revoke():
            with open(fifo, "rb") as handle:
                handle.read(1)
            sr.mark(self.spool, session, "revoke")

        thread = threading.Thread(target=revoke, daemon=True)
        thread.start()
        result = self.go(session, token, tuning=self.tuning(heartbeat_seconds=0.05))
        thread.join(10)
        self.assert_fixed(result, stage="gone")
        self.assertFalse((self.spool / session / "out" / "report.json").exists())
        self.assertTrue(sr.is_marked(self.spool, session, "revoke"))

    def test_a_stream_chunk_the_relay_refuses_with_a_cap_ends_the_session(self):
        self.claude(
            """
            import sys, time
            sys.stdin.buffer.read()
            sys.stdout.write("x" * 4096); sys.stdout.flush()
            time.sleep(30)
            """
        )
        scripted = ScriptedRelay(
            self,
            {
                ("POST", "/v1/preflight"): [Reply(200)],
                ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
                ("POST", "/v1/stream/transcript"): [Reply(413, b'{"error":"transcript_cap"}')],
                ("POST", "/v1/stream/stderr"): [Reply(200)],
                ("POST", "/v1/heartbeat"): [Reply(200)],
                ("POST", "/v1/result"): [Reply(200)],
            },
        )
        result = self.go(*self.session(), port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)))
        self.assert_fixed(result, stage="gone")
        self.assertEqual(scripted.requests("POST", "/v1/result"), [])


class Stopping(RunBase):
    """The time limit, the token deadline, the memory guard and SIGTERM."""

    def sleeper(self, ignore_term=False, child=False):
        lines = ["import os, signal, subprocess, sys, time", "sys.stdin.buffer.read()"]
        if ignore_term:
            lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
        if child:
            lines += [
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "sys.stderr.write('child=%d\\n' % child.pid)",
                "sys.stderr.flush()",
            ]
        lines += [
            "with open('written.txt', 'w') as handle:",
            "    handle.write('before the stop\\n')",
            "sys.stdout.write('started\\n')",
            "sys.stdout.flush()",
            "time.sleep(60)",
        ]
        self.claude("\n".join(lines) + "\n")

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            with open(f"/proc/{pid}/stat") as handle:
                return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    def test_a_session_that_reaches_its_time_limit_is_stopped_with_sigterm_and_reported_as_timeout(self):
        self.sleeper()
        manifest = self.manifest(run=self.run_section(timeout=2))
        session, token = self.session(manifest)
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        report = self.report(session)
        self.assertEqual((report["outcome"], report["exit_code"]), ("timeout", -signal.SIGTERM))
        self.assertEqual(report["delta"]["status"], "ok")
        self.assertIn(b"written.txt", self.out(session, "delta"))  # what the agent did before the limit still comes back
        self.assertIn("started", self.out(session, "transcript").decode())

    def test_an_agent_that_ignores_sigterm_is_killed_after_the_grace_and_so_is_everything_in_its_group(self):
        self.sleeper(ignore_term=True, child=True)
        manifest = self.manifest(run=self.run_section(timeout=2))
        session, token = self.session(manifest)
        result = self.go(session, token, tuning=self.tuning(stop_grace=0.3))
        self.assert_fixed(result, code=0)
        report = self.report(session)
        self.assertEqual((report["outcome"], report["exit_code"]), ("timeout", -signal.SIGKILL))
        pid = int(self.agent_stderr_lines(session)[0].split("=")[1])
        self.assertFalse(self.alive(pid))

    def test_leftovers_of_an_agent_that_exited_by_itself_are_killed_too(self):
        self.claude(
            """
            import subprocess, sys
            sys.stdin.buffer.read()
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sys.stderr.write("child=%d\\n" % child.pid)
            """
        )
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        self.assertFalse(self.alive(int(self.agent_stderr_lines(session)[0].split("=")[1])))

    def test_the_time_limit_is_cut_to_what_the_token_deadline_leaves_less_the_reserve(self):
        self.sleeper()
        session, token = self.session()
        env = self.environment(session, FACTORY_SESSION_DEADLINE=str(int(base.time.time()) + 3601))
        result = self.go(session, token, env=env, tuning=self.tuning(reserve=3599))  # between 1 and 2 seconds are left
        self.assert_fixed(result, code=0)
        self.assertEqual(self.report(session)["outcome"], "timeout")  # the manifest's 30 s were not what ended it

    def test_the_memory_guard_stops_an_agent_over_its_limit_and_the_report_says_so(self):
        self.sleeper()
        manifest = self.manifest(run=self.run_section(memory_limit_mb=1))
        session, token = self.session(manifest)
        result = self.go(session, token)
        self.assert_fixed(result, code=0)  # the guard's own fixed lines are not on the wrapper's streams
        report = self.report(session)
        self.assertEqual(report["outcome"], "memory_limit")
        self.assertGreaterEqual(report["exceeded_mb"], 1)
        self.assertEqual(report["footprint"]["limit_mb"], 1)
        self.assertGreaterEqual(report["footprint"]["samples"], 1)

    def test_a_line_the_memory_guard_prints_during_a_run_never_reaches_the_wrappers_streams(self):
        from factory import memwatch

        self.claude()
        session, token = self.session()
        start = memwatch.Watchdog.start

        def noisy(watchdog):
            memwatch.notice("factory: memory guard unavailable: POISON")
            return start(watchdog)

        with mock.patch.object(memwatch.Watchdog, "start", noisy):
            result = self.go(session, token)
        self.assert_fixed(result, code=0)

    def test_the_guards_own_lines_are_dropped_and_restored(self):
        from factory import memwatch

        saved = memwatch.notice
        err = io.StringIO()
        with contextlib.redirect_stderr(err), podworker._muted(memwatch):
            memwatch.notice("factory: memory guard unavailable")
            self.assertIsNot(memwatch.notice, saved)
        self.assertEqual(err.getvalue(), "")
        self.assertIs(memwatch.notice, saved)

    def test_sigterm_stops_the_agent_drains_the_streams_and_still_posts_the_delta_and_the_result(self):
        fifo = self.base / "ready"
        os.mkfifo(fifo)
        self.claude(
            f"""
            import sys, time
            sys.stdin.buffer.read()
            with open("written.txt", "w") as handle:
                handle.write("before the stop\\n")
            sys.stdout.write("started\\n"); sys.stdout.flush()
            open({os.fspath(fifo)!r}, "w").write("up")
            time.sleep(60)
            """
        )
        session, token = self.session()
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a stray signal must never end the test runner
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        seen = []

        def terminate():
            with open(fifo, "rb") as handle:
                handle.read(1)
            seen.append(signal.getsignal(signal.SIGTERM))
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)  # a second one while stopping changes nothing

        thread = threading.Thread(target=terminate, daemon=True)
        thread.start()
        result = self.go(session, token, tuning=self.tuning(signals=True))
        thread.join(10)
        self.assert_fixed(result, code=0)
        self.assertNotEqual(seen[0], signal.SIG_IGN)  # the wrapper's handler was installed before the agent started
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)  # and the previous one is back
        report = self.report(session)
        self.assertEqual((report["outcome"], report["exit_code"]), ("terminated", -signal.SIGTERM))
        self.assertEqual(self.state(session)["state"], "finished")
        self.assertIn(b"written.txt", self.out(session, "delta"))
        self.assertIn("started", self.out(session, "transcript").decode())
        self.assertEqual(result.out.splitlines()[-2:], ["podworker: exit 143", "podworker: result posted"])

    def test_sigterm_on_an_agent_that_ignores_it_ends_in_sigkill_after_the_grace_and_still_posts(self):
        fifo = self.base / "ready"
        os.mkfifo(fifo)
        self.claude(
            f"""
            import signal, sys, time
            sys.stdin.buffer.read()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            open({os.fspath(fifo)!r}, "w").write("up")
            time.sleep(60)
            """
        )
        session, token = self.session()
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

        def terminate():
            with open(fifo, "rb") as handle:
                handle.read(1)
            os.kill(os.getpid(), signal.SIGTERM)

        thread = threading.Thread(target=terminate, daemon=True)
        thread.start()
        result = self.go(session, token, tuning=self.tuning(signals=True, stop_grace=0.3))
        thread.join(10)
        self.assert_fixed(result, code=0)
        report = self.report(session)
        self.assertEqual((report["outcome"], report["exit_code"]), ("terminated", -signal.SIGKILL))

    def test_the_handler_is_not_installed_when_the_run_is_not_in_the_main_thread_and_the_run_still_works(self):
        self.claude()
        session, token = self.session()
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.go(session, token, tuning=self.tuning(signals=True))), daemon=True)
        thread.start()
        thread.join(60)
        self.assert_fixed(outcome[0], code=0)

    def test_the_default_handler_is_restored_after_the_run(self):
        self.claude()
        before = signal.getsignal(signal.SIGTERM)
        session, token = self.session()
        self.assert_fixed(self.go(session, token, tuning=self.tuning(signals=True)), code=0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


class Delta(RunBase):
    def test_a_delta_over_a_cap_is_reported_too_large_with_an_empty_body_and_nothing_is_truncated(self):
        self.claude(CLAUDE.replace('with open("README"', 'for n in range(5):\n    open("f%d.txt" % n, "w").write("x")\nwith open("README"'))
        session, token = self.session()
        small = {**bundle.LIMITS, bundle.DELTA: bundle.Limits(pc.DELTA_MAX_BYTES, 3)}
        with mock.patch.dict(bundle.LIMITS, small):
            result = self.go(session, token)
        self.assert_fixed(result, code=0)
        report = self.report(session)
        self.assertEqual(report["delta"], {"status": "too_large", "records": 0, "bytes": 0})
        self.assertEqual(self.out(session, "delta"), b"")
        self.assertEqual(report["outcome"], "exited")

    def test_a_tree_that_cannot_be_read_is_reported_failed_and_the_session_still_ends_cleanly(self):
        self.claude()
        session, token = self.session()
        with mock.patch.object(bundle, "produce_delta", side_effect=bundle.TreeError("changed")):
            result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(self.report(session)["delta"], {"status": "failed", "records": 0, "bytes": 0})
        self.assertEqual(self.out(session, "delta"), b"")

    def test_a_scratch_directory_that_cannot_be_used_is_reported_failed(self):
        self.claude()
        session, token = self.session()
        result = self.go(session, token, tuning=self.tuning(tmp_dir=os.fspath(self.base / "missing" / POISON)))
        self.assert_fixed(result, code=0)
        self.assertEqual(self.report(session)["delta"]["status"], "failed")

    def test_a_file_over_the_per_file_cap_is_too_large_and_a_fifo_in_the_tree_is_not_waited_on(self):
        self.claude(CLAUDE.replace('with open("README"', 'os.mkfifo("pipe")\nwith open("big", "wb") as big:\n    big.truncate(100)\nwith open("README"'))
        session, token = self.session()
        with mock.patch.object(bundle.walk, "__defaults__", (50, False)):  # the per-file cap of the walk
            result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(self.report(session)["delta"]["status"], "too_large")
        # the same tree without the cap: the fifo is skipped, never waited on, and is not in the delta
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        names = [record.path for record in bundle.Reader(io.BytesIO(self.out(session, "delta")), bundle.DELTA)]
        self.assertNotIn("pipe", names)
        self.assertIn("big", names)

    def test_the_agents_changes_to_the_pods_git_directory_never_come_back(self):
        self.claude(
            CLAUDE.replace(
                'with open("README"',
                'open(".git/config", "w").write("[core]\\n\\tfsmonitor = evil\\n")\nos.makedirs(".git/hooks")\nopen(".git/hooks/pre-commit", "w").write("evil")\nopen(".git/MERGE_HEAD", "w").write("x")\nwith open("README"',
            )
        )
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        for record in bundle.Reader(io.BytesIO(self.out(session, "delta")), bundle.DELTA):
            self.assertNotIn(".git", record.path)
        self.assertEqual([record.path for record in bundle.Reader(io.BytesIO(self.out(session, "delta")), bundle.DELTA)], ["README"])

    def test_a_read_only_phase_that_changed_nothing_returns_an_empty_delta_and_one_that_did_is_left_for_the_supervisor_to_refuse(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        with self.assertRaises(bundle.DeltaRejected) as caught:
            bundle.apply_delta(io.BytesIO(self.out(session, "delta")), self.base / "nowhere", write_scope="none")
        self.assertEqual(caught.exception.reason, "scope")  # the wrapper reports the truth; the supervisor enforces the scope

    def test_the_worktree_is_never_followed_when_it_has_been_swapped_for_a_link(self):
        (self.outside / "secret.txt").write_text("not the agent's to ship\n")
        self.claude(
            f"""
            import os, shutil, sys
            sys.stdin.buffer.read()
            os.chdir("..")
            shutil.rmtree("tree")
            os.symlink({os.fspath(self.outside)!r}, "tree")
            """
        )
        session, token = self.session()
        result = self.go(session, token)
        self.assert_fixed(result, code=0)
        self.assertEqual(self.report(session)["delta"]["status"], "failed")  # O_NOFOLLOW on the worktree: nothing outside it is read
        self.assertEqual(self.out(session, "delta"), b"")


class ResultPosting(RunBase):
    def scripted(self, results):
        self.claude("import sys\nsys.stdin.buffer.read()\n")
        return ScriptedRelay(
            self,
            {
                ("POST", "/v1/preflight"): [Reply(200)],
                ("GET", "/v1/bundle"): [Reply(200, make_bundle(self.manifest()))],
                ("POST", "/v1/stream/transcript"): [Reply(200)],
                ("POST", "/v1/stream/stderr"): [Reply(200)],
                ("POST", "/v1/heartbeat"): [Reply(200)],
                ("POST", "/v1/result"): results,
            },
        )

    def post(self, results):
        scripted = self.scripted(results)
        session, token = pc.new_session_id(), sr.new_token()
        result = self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)))
        return scripted, result

    def test_a_lost_answer_is_retried_with_the_waits_and_the_third_try_posts(self):
        scripted, result = self.post([Reply(503), Reply(500), Reply(200)])
        self.assert_fixed(result, code=0)
        self.assertEqual(len(scripted.requests("POST", "/v1/result")), 3)
        self.assertEqual(self.clock.sleeps[-2:], [0, 0])

    def test_the_default_waits_are_one_and_two_seconds_on_the_injected_clock(self):
        self.assertEqual(podworker.RESULT_WAITS, (1, 2))
        scripted, result = self.post_with_tuning([Reply(503), Reply(503), Reply(200)], self.tuning(result_waits=podworker.RESULT_WAITS))
        self.assert_fixed(result, code=0)
        self.assertEqual(self.clock.sleeps, [1, 2])

    def post_with_tuning(self, results, tuning):
        scripted = self.scripted(results)
        session, token = pc.new_session_id(), sr.new_token()
        return scripted, self.go(session, token, port=scripted.port, net=base.FakeNet(("127.0.0.1", scripted.port)), tuning=tuning)

    def test_three_failures_are_the_result_stage_and_no_exit_zero(self):
        scripted, result = self.post([Reply(502)])
        self.assert_fixed(result, stage="result")
        self.assertEqual(len(scripted.requests("POST", "/v1/result")), 3)
        self.assertIn("podworker: exit 0", result.out.splitlines())
        self.assertNotIn("podworker: result posted", result.out.splitlines())

    def test_a_410_is_gone_and_other_4xx_are_the_result_stage_and_neither_is_retried(self):
        scripted, result = self.post([Reply(410)])
        self.assert_fixed(result, stage="gone")
        self.assertEqual(len(scripted.requests("POST", "/v1/result")), 1)
        for status in (400, 403, 409, 413):
            with self.subTest(status=status):
                scripted, result = self.post([Reply(status)])
                self.assert_fixed(result, stage="result")
                self.assertEqual(len(scripted.requests("POST", "/v1/result")), 1)

    def test_an_unreachable_relay_at_the_end_is_the_result_stage(self):
        self.claude("import sys\nsys.stdin.buffer.read()\n")
        session, token = self.session()
        with mock.patch.object(podworker.RelayClient, "result", return_value=None):
            result = self.go(session, token)
        self.assert_fixed(result, stage="result")

    def test_a_report_that_cannot_be_built_is_the_result_stage_and_posts_nothing(self):
        self.claude()
        session, token = self.session()
        with mock.patch.object(podreport, "build", side_effect=ValueError(POISON)):
            result = self.go(session, token)
        self.assert_fixed(result, stage="result")
        self.assertFalse((self.spool / session / "out" / "report.json").exists())

    def test_the_real_relay_accepts_the_result_once_and_a_second_run_is_refused(self):
        self.claude()
        session, token = self.session()
        self.assert_fixed(self.go(session, token), code=0)
        again = self.go(session, token)
        self.assert_fixed(again, stage="preflight")


class Module(unittest.TestCase):
    SOURCE = base.SOURCE

    def test_the_exit_number_is_the_status_or_128_plus_the_signal_and_never_above_255(self):
        for returncode, number in ((0, 0), (3, 3), (255, 255), (300, 255), (-9, 137), (-15, 143), (-1, 129), (-200, 255)):
            with self.subTest(returncode=returncode):
                self.assertEqual(podworker.exit_number(returncode), number)

    def test_the_new_stages_are_fixed_distinct_and_away_from_126_127_and_the_signal_range(self):
        for stage, code in {"key": 78, "spawn": 79, "result": 80, "gone": 81}.items():
            self.assertEqual(podworker.STAGES[stage], code)
        self.assertLess(max(podworker.STAGES.values()), 126)

    def test_the_numbers_are_the_specified_ones(self):
        self.assertEqual(podworker.STOP_GRACE_SECONDS, 5)
        self.assertLessEqual(podworker.STOP_GRACE_SECONDS + podworker.TERMINATED_DRAIN_SECONDS + podworker.TERMINATED_FLUSH_SECONDS, pc.TERMINATION_GRACE_SECONDS)
        self.assertLessEqual(podworker.STREAM_SEND_BYTES, relay.STREAM_CHUNK_MAX_BYTES)
        self.assertEqual(podreport.MAX_BYTES, relay.REPORT_MAX_BYTES)
        self.assertEqual(podworker.STREAM_SLICE_BYTES, 64 * 1024)
        self.assertLess(podworker.RESULT_RESERVE_SECONDS, pc.DEADLINE_MARGIN_SECONDS)

    def test_the_source_reads_the_contracts_heartbeat_interval(self):
        self.assertIn("HEARTBEAT_SECONDS", self.SOURCE.read_text(encoding="utf-8"))

    def test_the_wrapper_starts_the_agent_only_through_popen_with_an_explicit_environment_and_no_shell(self):
        import ast

        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        popens = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                self.fail("a shell")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id in ("subprocess", "os"):
                    self.assertNotIn(node.func.attr, {"system", "run", "check_output", "check_call", "call", "spawnv", "execv", "execvp", "popen"})
                if node.func.attr == "popen":
                    popens += 1
                    self.assertIn("env", {keyword.arg for keyword in node.keywords})
        self.assertEqual(popens, 1)

    def test_nothing_prints_and_the_environment_is_only_read_by_main(self):
        import ast

        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn("print", {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})
        reads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
        ]
        self.assertEqual(len(reads), 1)  # the pod's own environment, read once by `main`; the child gets a copy of five names


class ProduceDelta(unittest.TestCase):
    """`bundle.produce_delta`, the pod side of the file delta, against real directories and the supervisor's own validator."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.pod = self.root / "pod"
        self.sup = self.root / "sup"
        self.pod.mkdir()
        self.sup.mkdir()
        self.files = {"README": b"hello\n", "src/a.py": b"print(1)\n", "run.sh": b"#!/bin/sh\n", "dir/b.txt": b"b\n"}
        sink = io.BytesIO()
        writer = bundle.Writer(sink)
        writer.put_manifest({"version": 1, "worktree": "tree"})
        for path, data in self.files.items():
            writer.put(bundle.FILE, bundle.AREA_TREE, path, data, path == "run.sh")
        for path, data in self.files.items():
            writer.put_baseline(path, bundle.FILE, hashlib.sha256(data).digest(), path == "run.sh")
        self.extracted = bundle.extract(io.BytesIO(sink.getvalue()), self.pod)
        self.tree = self.pod / "tree"
        for path, data in self.files.items():
            (self.sup / path).parent.mkdir(parents=True, exist_ok=True)
            (self.sup / path).write_bytes(data)
            if path == "run.sh":
                os.chmod(self.sup / path, 0o755)

    def produce(self):
        sink = io.BytesIO()
        fd = os.open(self.tree, os.O_RDONLY | os.O_DIRECTORY)
        try:
            produced = bundle.produce_delta(fd, self.extracted.baseline, sink)
        finally:
            os.close(fd)
        return produced, sink.getvalue()

    def test_an_untouched_tree_is_exactly_the_header(self):
        produced, data = self.produce()
        self.assertEqual(data, bundle.HEADER)
        self.assertEqual(produced, bundle.Produced(0, 0, 0, 0, len(bundle.HEADER)))

    def test_it_is_byte_for_byte_what_the_stand_in_of_the_bundle_tests_produced_and_applies(self):
        from tests import test_bundle

        (self.tree / "README").write_text("edited\n")
        (self.tree / "new").mkdir()
        (self.tree / "new" / "n.txt").write_text("n\n")
        os.unlink(self.tree / "src" / "a.py")
        os.chmod(self.tree / "run.sh", 0o644)
        os.symlink("README", self.tree / "link")
        os.makedirs(self.tree / "ignored" / "deep")
        (self.tree / "ignored" / "deep" / "x.bin").write_bytes(b"\0\1")
        produced, data = self.produce()
        self.assertEqual(data, test_bundle.pod_delta(self.tree, self.extracted.baseline))
        applied = bundle.apply_delta(
            io.BytesIO(data), self.sup, write_scope="cwd", declared_records=produced.records, declared_bytes=produced.size
        )
        self.assertEqual((applied.records, applied.files, applied.symlinks, applied.deleted), (produced.records, produced.files, produced.symlinks, produced.deleted))
        self.assertEqual((produced.files, produced.symlinks, produced.deleted), (4, 1, 1))
        self.assertEqual((self.sup / "README").read_text(), "edited\n")
        self.assertFalse((self.sup / "src" / "a.py").exists())
        self.assertEqual(stat.S_IMODE(os.lstat(self.sup / "run.sh").st_mode), 0o644)

    def test_a_file_that_grows_between_the_walk_and_the_read_fails_the_delta_instead_of_shipping_a_truncated_copy(self):
        (self.tree / "README").write_text("edited\n")  # a change, so the file is read for the record
        real_open = bundle.open_file

        def growing(entry):
            if entry.path == "README":
                with open(self.tree / "README", "ab") as handle:  # a process outside the group, after the walk's lstat
                    handle.write(b"more bytes than lstat counted\n")
            return real_open(entry)

        with mock.patch.object(bundle, "open_file", growing), self.assertRaises(bundle.TreeError) as caught:
            self.produce()
        self.assertEqual(caught.exception.reason, "changed")

    def test_a_rewritten_file_with_the_same_bytes_and_a_touched_one_are_not_changes(self):
        (self.tree / "README").write_bytes(b"hello\n")
        os.utime(self.tree / "README", (1, 1))
        self.assertEqual(self.produce()[1], bundle.HEADER)

    def test_a_kind_change_is_a_change_a_file_that_became_a_link_and_a_link_that_became_a_file(self):
        os.unlink(self.tree / "README")
        os.symlink("src/a.py", self.tree / "README")
        produced, data = self.produce()
        self.assertEqual((produced.symlinks, produced.files, produced.deleted), (1, 0, 0))
        bundle.apply_delta(io.BytesIO(data), self.sup, write_scope="cwd")
        self.assertEqual(os.readlink(self.sup / "README"), "src/a.py")

    def test_the_git_directory_of_every_spelling_and_the_special_files_are_never_in_it(self):
        (self.tree / ".git").mkdir(exist_ok=True)
        (self.tree / ".git" / "config").write_text("evil")
        (self.tree / "nested" / ".GIT").mkdir(parents=True)
        (self.tree / "nested" / ".GIT" / "config").write_text("evil")
        os.mkfifo(self.tree / "fifo")
        _produced, data = self.produce()
        self.assertEqual(data, bundle.HEADER)

    def test_a_delta_over_its_caps_is_too_large_and_nothing_is_truncated(self):
        for index in range(4):
            (self.tree / f"f{index}").write_text("x")
        small = {**bundle.LIMITS, bundle.DELTA: bundle.Limits(pc.DELTA_MAX_BYTES, 3)}
        with mock.patch.dict(bundle.LIMITS, small), self.assertRaises(bundle.BundleTooLarge):
            self.produce()
        small = {**bundle.LIMITS, bundle.DELTA: bundle.Limits(60, pc.DELTA_MAX_FILES)}
        with mock.patch.dict(bundle.LIMITS, small), self.assertRaises(bundle.BundleTooLarge):
            self.produce()

    def test_a_tree_that_cannot_be_expressed_or_a_file_over_the_cap_fails_instead_of_being_skipped_silently(self):
        (self.tree / "big").write_bytes(b"x" * 100)
        with mock.patch.object(bundle.walk, "__defaults__", (50, False)), self.assertRaises(bundle.BundleTooLarge):
            self.produce()

    def test_the_stream_is_a_valid_delta_for_the_reader_and_has_no_other_area_or_kind(self):
        (self.tree / "README").write_text("edited\n")
        os.unlink(self.tree / "dir" / "b.txt")
        _produced, data = self.produce()
        records = list(bundle.Reader(io.BytesIO(data), bundle.DELTA))
        self.assertEqual({record.area for record in records}, {bundle.AREA_TREE})
        self.assertEqual({record.kind for record in records}, {bundle.FILE, bundle.DELETE})


if __name__ == "__main__":
    unittest.main()
