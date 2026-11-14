"""Phase 5 end to end: kill the supervisor mid-build, restart, recover, retry, run, purge.

The acceptance of the restart behaviour, the auto-purge and the cost guardrail on
real pieces, with fakes only for the outside world:

REAL: the ``Manager`` with its ``SubprocessRunner`` and ``SubprocessSpawner`` (the CLI is
``python -m factory`` with an absolute ``PYTHONPATH``), the HTTP server (``make_server`` on
127.0.0.1 port 0, requests carry an Operator principal), the ``Operations`` loop, the CLI
(``plan``, ``run``, ``status --json``, ``recover``, ``retry``, ``drain``, ``purge``,
``purge-clone``), real git (a bare origin, a managed clone, worktrees), and the repository
lease: ``BlobLeaseLock`` in the supervisor subprocess, in the CLI commands and in the manager,
authenticated by ``ManagedIdentityToken``.

FAKE: the Azure blob service (``FakeBlobServer``, whose lease clock is a ``FakeClock`` that
only moves when a test advances it, so "the killed supervisor stops renewing" is "the clock
moves past the 60 s lease"), the managed identity endpoint (a tiny local HTTP server behind
``IDENTITY_ENDPOINT`` / ``IDENTITY_HEADER``), ``gh`` (``FAKE_GH`` of tests/test_factory.py plus
``pr view``, which the status refresh needs to see a merged PR) and the ``claude`` agent (a
script whose build step writes a marker and blocks until a file exists).

Waiting is never a fixed sleep: every wait is a condition polled against a deadline. The
operations loop of the "restart" manager is driven pass by pass through its injectable clock
and wait hooks (one test uses the real threads of ``Manager.start_operations``). Every test
ends by proving that neither the issue text, the prompts, nor the agent's output reached the
manager's stdout/stderr, the HTTP replies, the captured CLI output or the supervisor logs.
"""

import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import factory
from factory import manager as mgr
from factory.config import data_paths, load_repo_config
from factory.manager import (
    Manager,
    Settings,
    SubprocessRunner,
    SubprocessSpawner,
    make_server,
)
from factory.operations import BUDGET_MARKER, RESUME_MARKER, RESUME_REASON, Operations
from factory.state import host_id

from tests import agentstub
from tests import test_factory as fixtures
from tests.fake_blob import FakeBlobServer, FakeClock
from tests.test_manager import OPERATOR

SRC = str(Path(factory.__file__).resolve().parent.parent)
REPO, RUN, JOB = "org/name", "r1", "fix-a"
BASE = f"/repos/{REPO}/runs/{RUN}"
CLI = (sys.executable, "-m", "factory")
PR_URL = "https://example.invalid/pr/1"
LEASE_SECONDS = 60
# A pid recorded by a replica that is not this host: it can never be probed from here.
OTHER_HOST = "replica-b/11111111-2222-3333-4444-555555555555"
# Every wait is bounded; a broken test fails with diagnostics instead of hanging CI.
BOUND = 90.0

# Text that must stay in files under the run directory and never reach a log or a reply: a
# canary that is in the issue body (so in the planner and builder prompts) and in the agent's
# own output, and sentences of the prompt templates.
CANARY = "CANARY-5f3a9c-issue-and-agent-text"
PROMPT_FRAGMENTS = (
    "Implement the assignment in this dedicated git worktree",
    "You are planning a small GitHub issue factory",
    "You are the independent reviewer",
    "Issue evidence:",
    "Return ONLY JSON",
)
FORBIDDEN = (CANARY, *PROMPT_FRAGMENTS)

# What the planner and the supervisor print to their log files (their stdout and stderr).
PLAN_LINE = re.compile(r"^Planned \d+ issues in \d+ jobs: \S+$")
SUPERVISOR_LINE = re.compile(
    rf"^(?:{JOB}: (?:starting [a-z0-9-]+|pr_ready \S+|waiting_[a-z]+|queued|interrupted)"
    r"|factory: reason_code job=[a-z0-9-]+ issues=(?:[\d,]+|none) commit=[0-9a-f]{7} "
    r"code=(?:real_failure|ci_failed|ci_pending|env_noise|unverified_other|none)"
    r"(?: [a-z_]+=\d+){5}"
    r"|Supervisor drained; checkpoints retained\. Drain does not mark work complete\. Inspect status, "
    r"use recovery commands, then explicitly run again\.)$"
)

PLAN = {
    "version": 1,
    "jobs": [
        {
            "id": JOB,
            "issues": [1],
            "context_issues": [],
            "objective": "Fix A",
            "likely_paths": ["src"],
            "acceptance": ["A works"],
            "depends_on": [],
            "feature": False,
        }
    ],
    "dispositions": [{"issue": 1, "status": "job", "job": JOB, "reason": "Concrete fix"}],
}

# ``gh pr view`` (the status refresh asks it about every published PR) on top of the shared fake.
PR_VIEW = r"""
import json, os, pathlib, sys
if sys.argv[1:3] == ['pr', 'view']:
    path = pathlib.Path(os.environ['FAKE_PR'])
    prs = json.loads(path.read_text()) if path.exists() else []
    with open(os.environ['FAKE_GH_LOG'], 'a') as log:
        log.write(json.dumps({'args': sys.argv[1:], 'token': os.environ.get('GH_TOKEN')}) + '\n')
    if not prs:
        sys.exit(1)
    pr = prs[0]
    print(json.dumps({
        'headRefOid': pr.get('headRefOid'),
        'baseRefName': pr.get('baseRefName'),
        'isDraft': pr.get('isDraft', False),
        'state': pr['state'],
        'statusCheckRollup': [],
        'mergeCommit': None,
    }))
    sys.exit(0)
"""

# The agent: the planner answers with PLAN; the reviewer passes (and, if ``hold-review`` exists,
# blocks until ``go-review`` exists); the builder records its pid in ``build.started``, blocks
# until ``go`` exists, then makes the fix. Blocking is bounded, so a broken test cannot hang CI.
AGENT_BODY = r"""
import time
MARKERS = pathlib.Path(__MARKERS__)
CANARY = __CANARY__
PLAN = json.loads(__PLAN__)
prompt = sys.stdin.read()


def started(name):
    with (MARKERS / name).open('a') as handle:
        handle.write(str(os.getpid()) + '\n')


def hold_until(name):
    deadline = time.monotonic() + 60
    while not (MARKERS / name).exists():
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.05)


print('agent chatter ' + CANARY, file=sys.stderr, flush=True)
if 'planning a small GitHub issue factory' in prompt:
    emit(PLAN)
elif 'independent reviewer' in prompt:
    started('review.started')
    if (MARKERS / 'hold-review').exists():
        hold_until('go-review')
    emit({'pass': True, 'findings': []})
else:
    started('build.started')
    hold_until('go')
    pathlib.Path('src/a.txt').write_text('fixed\n')
    emit({'summary': 'Fix A ' + CANARY, 'blocked': False, 'blocker': '', 'tests': ['t'], 'feature': False})
"""


def agent_script(markers):
    body = (
        AGENT_BODY.replace("__MARKERS__", repr(str(markers)))
        .replace("__CANARY__", repr(CANARY))
        .replace("__PLAN__", repr(json.dumps(PLAN)))
    )
    return agentstub.source(body)


class IdentityEndpoint:
    """Stands in for the Container Apps managed identity endpoint behind IDENTITY_ENDPOINT."""

    HEADER = "identity-header-not-a-secret"

    def __init__(self):
        self.requests = []  # (path, whether the X-IDENTITY-HEADER was the right one)
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                good = self.headers.get("X-IDENTITY-HEADER") == endpoint.HEADER
                endpoint.requests.append((self.path, good))
                if good:
                    status, body = 200, {
                        "access_token": "t",
                        "expires_on": str(int(time.time()) + 3600),
                        "token_type": "Bearer",
                    }
                else:
                    status, body = 401, {"error": "bad identity header"}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Httpd(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self._httpd = Httpd(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/msi/token"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(5)


class WaitHook:
    """The ``wait`` an ``Operations`` is given: records the request and never sleeps."""

    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        if len(self.waits) >= 100:  # a loop that never gives up fails the test instead of hanging it
            raise AssertionError("runaway wait loop")
        self.waits.append(seconds)
        return False


def command_of(argv):
    """The CLI subcommand of ``python -m factory --repo R [--run-dir D] <command> ...``."""
    rest = list(argv[len(CLI):])
    while rest and rest[0] in ("--repo", "--run-dir"):
        rest = rest[2:]
    return rest[0]


class RecordingSpawner(SubprocessSpawner):
    """The real spawner; it only notes every process it was asked to start."""

    def __init__(self, env, cwd):
        super().__init__(env, cwd)
        self.calls = []

    def __call__(self, argv, log_path):
        self.calls.append((list(argv), Path(log_path)))
        return super().__call__(argv, log_path)

    def commands(self):
        return [command_of(argv) for argv, _ in self.calls]


class TracedManager(Manager):
    """The real manager; every short CLI command is noted, and a test may look just before one runs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results = []  # (command, CommandResult) of every command that ran, in order
        self.before = []  # callables (command, argv), called just before a command runs

    def run_short(self, argv, lane="other"):
        name = command_of(argv)
        for hook in list(self.before):
            hook(name, argv)
        result = super().run_short(argv, lane)
        self.results.append((name, result))
        return result


class Instance:
    """One manager as a deployment would run it: the real manager and its HTTP server."""

    def __init__(self, case, manager, server, out, err, spawner):
        self.case, self.manager, self.server = case, manager, server
        self.out, self.err, self.spawner = out, err, spawner
        self.port = server.server_address[1]
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.ops = None  # an Operations driven pass by pass, when the test asked for one
        self.wait = None

    # -- observation ----------------------------------------------------------------

    def lines(self, prefix=""):
        return [line for line in self.out.getvalue().splitlines() if line.startswith(prefix)]

    def commands(self, since=0):
        return [name for name, _ in self.manager.results[since:]]

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=data, headers={mgr.PRINCIPAL_HEADER: OPERATOR})
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        self.case.http_bodies.append(raw.decode("utf-8", "replace"))
        return response.status, (json.loads(raw) if raw else None)

    # -- going away -----------------------------------------------------------------

    def stop_server(self):
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.close_connections()
            server.server_close()
            self.thread.join(10)

    def discard(self):
        """Forget this manager the way a dead container is forgotten.

        Its server stops answering, but nothing is signalled and ``terminate_children`` does
        not run, so whatever it started is left as it was. The only other thing done is to wake
        the waits of its operations threads (if it has any), which are not daemons and would
        otherwise outlive the test.
        """
        self.stop_server()
        operations = self.manager.operations
        if operations is not None and operations.threads:
            self.manager.closing.set()
            operations.join(10)

    def close(self):
        """Test cleanup: stop the server and take down whatever this manager still runs."""
        self.stop_server()
        self.manager.terminate_children(grace=5)


class OperationsIntegration(unittest.TestCase):
    """The deployment in a temp directory: config, data, origin, clone, lease, identity, gh, agent."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.markers = self.root / "markers"
        self.config_dir, self.data_dir = self.root / "config", self.root / "data"
        for directory in (self.bin, self.markers, self.config_dir / "repos", self.data_dir):
            directory.mkdir(parents=True)
        self.http_bodies = []  # every reply of the manager API
        self.logs = {}  # label -> text of a child log, read while it existed
        self.instances = []
        self.addCleanup(self.kill_stray_agents)

        self.git_env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.write_script("gh", PR_VIEW + fixtures.FAKE_GH)
        self.write_script("fake-agent", agent_script(self.markers))
        self.pr_path = self.root / "pr.json"
        (self.root / "issues.json").write_text(
            json.dumps([{"number": 1, "title": "Fix A", "body": f"A {CANARY}", "comments": [], "labels": [], "url": "u"}])
        )
        self.write_config()
        self.make_repositories()

        # The lease service and the identity endpoint. The manager's own lock reads the
        # identity variables from the process environment (like the deployed manager's),
        # the supervisor and CLI children get them in their environment.
        self.blobs = FakeBlobServer(clock=FakeClock()).start()
        self.addCleanup(self.blobs.stop)
        self.identity = IdentityEndpoint()
        self.addCleanup(self.identity.stop)
        patcher = patch.dict(
            os.environ, {"IDENTITY_ENDPOINT": self.identity.url, "IDENTITY_HEADER": IdentityEndpoint.HEADER}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = self.child_environment()

    # -- fixtures -------------------------------------------------------------------

    def write_script(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def write_config(self, extra=""):
        (self.config_dir / "repos" / "org__name.toml").write_text(f'repo = "{REPO}"\nbase_branch = "main"\n{extra}\n')

    def git(self, *args, cwd):
        done = subprocess.run(
            ["git", *args], cwd=cwd, env=self.git_env, text=True, capture_output=True, check=True, timeout=60
        )
        return done.stdout.strip()

    def make_repositories(self):
        """A bare origin with one commit on main and the managed clone of it, as ``plan`` finds them."""
        self.origin = self.root / "origin.git"
        seed = self.root / "seed"
        seed.mkdir()
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.root)
        self.git("init", "-q", "-b", "main", cwd=seed)
        for key, value in (("user.email", "factory@example.invalid"), ("user.name", "Factory Test")):
            self.git("config", key, value, cwd=seed)
        (seed / "src").mkdir()
        (seed / "src" / "a.txt").write_text("before\n")
        self.git("add", ".", cwd=seed)
        self.git("commit", "-q", "-m", "base", cwd=seed)
        self.git("remote", "add", "origin", str(self.origin), cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        cfg = load_repo_config(REPO, config_dir=self.config_dir)
        self.paths = data_paths(cfg, data_dir=self.data_dir)
        self.clone = Path(self.paths.clone)
        self.run_dir = self.paths.run_dir(RUN)
        self.state_path = self.run_dir / "state.json"
        self.clone.parent.mkdir(parents=True)
        self.git("clone", "-q", str(self.origin), str(self.clone), cwd=self.root)
        for key, value in (("user.email", "factory@example.invalid"), ("user.name", "Factory Test")):
            self.git("config", key, value, cwd=self.clone)

    def child_environment(self):
        """What the deployed manager passes to every CLI it starts: the lease, gh and the agent are fakes."""
        dropped = ("FACTORY_", "FAKE_", "IDENTITY_", "AZURE_", "GIT_", "GH_", "GITHUB_", "CLAUDE", "ANTHROPIC_")
        env = {k: v for k, v in os.environ.items() if not k.startswith(dropped) and k != "PYTHONPATH"}
        env.update(
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            PYTHONPATH=SRC,
            FACTORY_CONFIG=str(self.config_dir),
            FACTORY_DATA=str(self.data_dir),
            FACTORY_AGENT_CMD=str(self.bin / "fake-agent"),
            FACTORY_LOCK_ACCOUNT_URL=self.blobs.url,
            FACTORY_LOCK_CONTAINER="locks",
            IDENTITY_ENDPOINT=self.identity.url,
            IDENTITY_HEADER=IdentityEndpoint.HEADER,
            FAKE_PR=str(self.pr_path),
            FAKE_GH_LOG=str(self.root / "gh-calls.jsonl"),
            FAKE_ISSUES=str(self.root / "issues.json"),
            FAKE_DEFAULT_BRANCH="main",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        return env

    def start_manager(self, operations="off", **overrides):
        """A manager and its server. ``operations`` is "off", "hooks" (an ``Operations`` the test
        drives pass by pass, with injected wait and clock) or "threads" (``start_operations``, as
        ``serve`` does)."""
        out, err = io.StringIO(), io.StringIO()
        spawner = RecordingSpawner(self.env, self.data_dir)
        settings = {
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "host": "127.0.0.1",
            "port": 0,
            "cli": CLI,
            "command_timeout": 120.0,
            "shutdown_seconds": 5.0,
            "operations": operations != "off",
            # The loops that tick on their own never get to: tests call the passes themselves.
            "poll_seconds": 3600.0,
            "janitor_seconds": 3600.0,
            **overrides,
        }
        manager = TracedManager(
            Settings(**settings),
            env=self.env,
            runner=SubprocessRunner(self.env, self.data_dir),
            spawner=spawner,
            stdout=out,
            stderr=err,
        )
        instance = Instance(self, manager, make_server(manager), out, err, spawner)
        self.instances.append(instance)
        self.addCleanup(instance.close)
        if operations == "hooks":
            instance.wait = WaitHook()
            instance.ops = Operations(manager, wait=instance.wait, lease_poll=1.0)
            manager.operations = instance.ops
        elif operations == "threads":
            manager.start_operations()
        return instance

    # -- waiting --------------------------------------------------------------------

    def wait_until(self, condition, what, timeout=BOUND):
        deadline = time.monotonic() + timeout
        while True:
            value = condition()
            if value:
                return value
            if time.monotonic() >= deadline:
                self.fail(f"timed out after {timeout:g}s waiting for {what}\n{self.diagnostics()}")
            time.sleep(0.05)

    def diagnostics(self):
        parts = []
        for name in ("plan.log", "supervisor.log"):
            path = self.run_dir / name
            if path.is_file():
                parts.append(f"{name}: {path.read_text(errors='replace')[-1500:]!r}")
        for number, instance in enumerate(self.instances):
            parts.append(f"manager {number} stderr: {instance.err.getvalue()[-1500:]!r}")
        return "\n".join(parts)

    def wait_child_exit(self, instance, kind):
        def exited():
            if instance.manager.children.get(REPO, RUN) is not None:
                return None
            rows = instance.manager.children.last_exit(REPO, RUN, limit=1)
            return rows[-1] if rows and rows[-1]["kind"] == kind else None

        return self.wait_until(exited, f"the {kind} process to exit")

    def marker_pids(self, name):
        try:
            text = (self.markers / name).read_text()
        except FileNotFoundError:
            return []
        return [int(line) for line in text.splitlines() if line.strip().isdigit()]

    def wait_for_marker(self, name, count):
        return self.wait_until(lambda: len(self.marker_pids(name)) >= count, f"{count} line(s) in {name}")

    def release(self, name):
        (self.markers / name).write_text("go\n")

    # -- the run --------------------------------------------------------------------

    def plan(self, instance):
        status, payload = instance.call("POST", f"{BASE}/plan", {"limit": 5})
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.wait_child_exit(instance, "plan")["returncode"], 0, self.diagnostics())
        self.assertTrue((self.run_dir / "plan.json").is_file())
        self.read_logs("planned")

    def start_run(self, instance):
        status, payload = instance.call("POST", f"{BASE}/run", {"concurrency": 1, "timeout": 300})
        self.assertEqual(status, 202, payload)
        return payload["pid"]

    def job(self):
        return json.loads(self.state_path.read_text())["jobs"][JOB]

    def status(self, instance):
        status, payload = instance.call("GET", f"{BASE}/status")
        self.assertEqual((status, payload["returncode"]), (200, 0), payload)
        return payload["status"]

    def read_logs(self, label):
        for name in ("plan.log", "supervisor.log"):
            path = self.run_dir / name
            if path.is_file():
                self.logs[f"{label}/{name}"] = path.read_text(errors="replace")

    def crash_mid_build(self, instance, build_number):
        """SIGKILL the supervisor's group, then the blocked agent's, while build ``build_number`` waits.

        Nothing is signalled gracefully: the supervisor does not release its lease and does
        not get to record the agent's death. Afterwards the recorded pid belongs to "another
        host", the way a pid of a replaced container does: it cannot be probed, so recovery
        needs the lease.
        """
        self.wait_for_marker("build.started", build_number)
        child = instance.manager.children.get(REPO, RUN)
        self.assertIsNotNone(child)
        self.assertEqual(child.kind, "run")
        job = self.job()
        agent = self.marker_pids("build.started")[build_number - 1]
        self.assertEqual((job["status"], job["pid"], job["pid_host"]), ("running", agent, host_id()))
        os.killpg(child.pid, signal.SIGKILL)  # the supervisor first: it must not see the agent die
        os.killpg(agent, signal.SIGKILL)
        self.assertEqual(child.process.wait(30), -signal.SIGKILL)
        data = json.loads(self.state_path.read_text())
        data["jobs"][JOB]["pid_host"] = OTHER_HOST
        temp = self.state_path.with_name("state.rewrite")
        temp.write_text(json.dumps(data))
        os.replace(temp, self.state_path)

    def kill_stray_agents(self):
        """Last resort for a failed test: an agent that is still blocked on its marker."""
        for name in ("build.started", "review.started"):
            for pid in self.marker_pids(name):
                try:
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()  # only read: nothing runs it
                except OSError:
                    continue
                if str(self.bin / "fake-agent").encode() in cmdline:
                    with contextlib.suppress(ProcessLookupError):  # already gone: the point of the cleanup
                        os.killpg(pid, signal.SIGKILL)

    # -- observation of the repository ----------------------------------------------

    def tree(self, root):
        """Every file below ``root`` (git's own directories aside) with its digest."""
        found = {}
        for current, directories, files in os.walk(root):
            directories[:] = [name for name in directories if name != ".git"]
            for name in files:
                path = Path(current, name)
                found[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found

    def clone_worktrees(self):
        text = self.git("worktree", "list", "--porcelain", cwd=self.clone)
        listed = [Path(line.split(" ", 1)[1]) for line in text.splitlines() if line.startswith("worktree ")]
        return listed[1:]  # the first entry is the clone itself

    def branches(self, repository, prefix):
        return self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}", cwd=repository).split()

    def refused_acquires(self):
        return len([call for call in self.blobs.calls("acquire") if call.status == 409])

    def gh_log(self):
        path = self.root / "gh-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def merge_the_pr(self):
        prs = json.loads(self.pr_path.read_text())
        prs[0]["state"] = "MERGED"
        self.pr_path.write_text(json.dumps(prs))

    # -- hygiene ----------------------------------------------------------------------

    def assert_text_travelled(self):
        """Positive control: the canary and the prompts really are in the files that hold them."""
        prompts = sorted(self.run_dir.glob(f"{JOB}/*-build-0.prompt.txt"))
        self.assertTrue(prompts)
        built = prompts[-1].read_text()
        self.assertIn(CANARY, built)
        self.assertIn(PROMPT_FRAGMENTS[0], built)
        planner = (self.run_dir / "planner.prompt.txt").read_text()
        self.assertIn(CANARY, planner)
        self.assertIn(PROMPT_FRAGMENTS[1], planner)
        self.assertIn(CANARY, max(self.run_dir.glob(f"{JOB}/*-build-0.stderr")).read_text())
        self.assertIn(CANARY, self.state_path.read_text())  # the builder's summary
        self.assertIn(CANARY, json.dumps(self.gh_log()))  # and the PR body that went to GitHub

    def assert_no_leaks(self):
        """The canary and the prompts reached no log, reply or captured CLI output of any manager."""
        self.read_logs("final")
        sources = [(f"HTTP reply {n}", text) for n, text in enumerate(self.http_bodies)]
        sources += list(self.logs.items())
        for number, instance in enumerate(self.instances):
            sources.append((f"manager {number} stdout", instance.out.getvalue()))
            sources.append((f"manager {number} stderr", instance.err.getvalue()))
            for name, result in instance.manager.results:
                sources.append((f"manager {number} {name} stdout", result.stdout.decode("utf-8", "replace")))
                sources.append((f"manager {number} {name} stderr", result.stderr.decode("utf-8", "replace")))
        self.assertTrue(sources)
        for label, text in sources:
            for fragment in FORBIDDEN:
                if fragment in text:
                    self.fail(f"{fragment!r} reached {label}")
        for number, instance in enumerate(self.instances):
            # The CLI commands the manager ran say only what they did, nothing else.
            for name, result in instance.manager.results:
                self.assertEqual(result.stderr, b"", f"manager {number} {name}")
        # The children say only what they did: ids and statuses, never what the agent wrote.
        for label, text in self.logs.items():
            pattern = SUPERVISOR_LINE if label.endswith("supervisor.log") else PLAN_LINE
            for line in text.splitlines():
                self.assertRegex(line, pattern, label)
        return True


class KillMidBuildTests(OperationsIntegration):
    def test_kill_mid_build_then_recover_retry_run_and_purge(self):
        self.write_config()
        first = self.start_manager()
        self.plan(first)
        self.start_run(first)
        self.wait_for_marker("build.started", 1)

        # The supervisor holds the repository lease on the (fake) blob service, with a fence.
        blob = self.blobs.blob("locks", "org__name")
        self.assertEqual(blob.state, "leased")
        self.assertGreaterEqual(int(blob.metadata["fence"]), 1)
        self.assertTrue(self.identity.requests)
        self.assertTrue(all(good for _, good in self.identity.requests))
        self.assertTrue(all("resource=" in path for path, _ in self.identity.requests))

        # (2) Kill it, nobody releases anything, and the first manager is forgotten.
        self.crash_mid_build(first, 1)
        first.discard()
        self.assertEqual(first.manager.children.running(), [])
        exit_ = first.manager.children.last_exit(REPO, RUN, limit=1)[-1]
        self.assertEqual((exit_["kind"], exit_["returncode"]), ("run", -signal.SIGKILL))
        self.assertEqual(self.blobs.blob("locks", "org__name").state, "leased")  # nobody gave it up
        self.assertEqual(self.job()["pid_host"], OTHER_HOST)
        self.assertEqual(self.job()["status"], "running")

        # (4a) A new manager starts while the dead supervisor's lease is still valid: recovery
        # waits for it, gives up, reports the busy lease and modifies nothing.
        second = self.start_manager("hooks", lease_wait_seconds=3.0)
        tree, state = self.tree(self.data_dir), self.state_path.read_bytes()
        refused = self.refused_acquires()
        second.ops.recover_all()
        self.assertEqual(second.wait.waits, [1.0, 1.0, 1.0])
        self.assertEqual(self.refused_acquires() - refused, 4)  # the first probe and three polls
        self.assertEqual(second.manager.results, [])  # not one CLI command ran
        self.assertEqual(second.spawner.calls, [])
        self.assertEqual(second.lines("restart"), [])
        self.assertEqual(
            second.err.getvalue().splitlines(),
            [f"factory-manager: restart repo={REPO} lease busy after 3s; recovery skipped"],
        )
        self.assertEqual(self.state_path.read_bytes(), state)
        self.assertEqual(self.tree(self.data_dir), tree)

        # (4b) The lease expires (the killed supervisor stops renewing): recovery marks the job
        # interrupted and starts nothing, because auto_resume_after_restart is not set.
        self.blobs.clock.advance(LEASE_SECONDS + 1)
        second.ops.recover_all()
        self.assertEqual(second.commands(), ["recover", "status"])
        self.assertEqual(
            second.manager.results[0][1].stdout, b"Recovered 1 interrupted job(s)\n"
        )
        [line] = second.lines("restart")
        self.assertRegex(line, rf"^restart repo={REPO} run={RUN} interrupted=1 .* running=0 .* recovered=1$")
        self.assertEqual(second.spawner.calls, [])
        self.assertEqual(second.manager.children.running(), [])
        self.assertFalse((self.run_dir / RESUME_MARKER).exists())
        report = self.status(second)
        self.assertEqual([(row["id"], row["status"]) for row in report["jobs"]], [(JOB, "interrupted")])
        self.assertEqual(report["active_worker_count"], 0)
        self.assertEqual(report["supervisor"]["status"], "stale")
        self.assertEqual((self.job()["pid"], self.job()["pid_host"]), (None, None))

        # (5) The operator retries with a reason and runs again, through the HTTP API.
        reason = "the supervisor was killed in the middle of the build"
        self.release("go")  # the build that follows will not block
        status, payload = second.call("POST", f"{BASE}/retry", {"job": JOB, "reason": reason})
        self.assertEqual((status, payload["returncode"]), (200, 0), payload)
        self.assertEqual(payload["output"], f"{JOB} queued; existing worktree and branch will be reused\n")
        pid = self.start_run(second)
        self.assertEqual(second.spawner.commands(), ["run"])
        self.assertEqual(self.wait_child_exit(second, "run")["returncode"], 0, self.diagnostics())
        self.assertEqual(self.wait_child_exit(second, "run")["pid"], pid)
        self.assertEqual(len(self.marker_pids("build.started")), 2)  # the build ran again, from the retry
        report = self.status(second)
        [row] = report["jobs"]
        self.assertEqual((row["status"], row["pr"], row["attempt"], row["retries"]), ("pr_ready", PR_URL, 2, 1))
        self.assertGreater(report["cost_usd"], 0)
        self.assertEqual(self.job()["retry_history"][-1]["reason"], reason)
        # The reason is in the state, in the audit line only as its length.
        [retry_audit] = [line for line in second.lines("audit") if "/retry" in line]
        self.assertIn(f"--reason=<{len(reason)} chars>", retry_audit)
        self.assertNotIn(reason, second.out.getvalue())
        self.read_logs("finished")
        self.assertIn(f"{JOB}: pr_ready {PR_URL}", self.logs["finished/supervisor.log"])
        self.assert_text_travelled()

        # (6) The janitor leaves a run whose PR is open alone ...
        worktrees, branches = self.clone_worktrees(), self.branches(self.clone, f"factory/{RUN}/")
        self.assertTrue(all(path.is_relative_to(self.run_dir) for path in worktrees))
        self.assertIn(f"factory/{RUN}/{JOB}", branches)
        self.assertGreaterEqual(len(worktrees), 2)
        commit = self.job()["commit"]
        mark = len(second.manager.results)
        second.ops.sweep()
        # (the open PR is also looked at once, for the idle reconcile of its status label)
        self.assertEqual(second.commands(mark), ["status", "reconcile"])
        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual(second.lines("purge"), [])

        # ... and when the PR is merged the run is terminal: purge, then the clone, as it was the last run.
        self.merge_the_pr()
        between = {}

        def look_before_the_clone_goes(name, argv):
            if name == "purge-clone":
                between["worktrees"] = self.clone_worktrees()
                between["branches"] = self.branches(self.clone, f"factory/{RUN}/")

        second.manager.before.append(look_before_the_clone_goes)
        mark = len(second.manager.results)
        second.ops.sweep()
        self.assertEqual(second.commands(mark), ["status", "purge", "purge-clone"])
        _, purged, removed = [result for _, result in second.manager.results[mark:]]
        self.assertEqual(
            purged.stdout,
            f"Purged run {RUN}: {len(worktrees)} worktrees, {len(branches)} branches, run dir removed=True\n".encode(),
        )
        self.assertEqual(removed.stdout, b"Removed clone\n")
        self.assertEqual(second.lines("purge"), [
            f"purge repo={REPO} run={RUN} reason=terminal",
            f"purge-clone repo={REPO} removed=yes",
        ])
        self.assertFalse(self.run_dir.exists())
        # Between the purge and the removal of the clone: none of the run's worktrees or branches left in it.
        self.assertEqual(between, {"worktrees": [], "branches": []})
        self.assertFalse(self.clone.exists())
        # The remote branch is never touched.
        self.assertEqual(self.branches(self.origin, f"factory/{RUN}/{JOB}"), [f"factory/{RUN}/{JOB}"])
        self.assertEqual(self.git("rev-parse", f"factory/{RUN}/{JOB}", cwd=self.origin), commit)
        status, payload = second.call("GET", "/repos")
        self.assertEqual([row["runs"] for row in payload["repos"] if row["repo"] == REPO], [[]])
        # Another tick finds nothing to do.
        mark = len(second.manager.results)
        second.ops.sweep()
        self.assertEqual(second.commands(mark), [])

        self.assertTrue(self.assert_no_leaks())


    def test_the_threads_of_a_restarted_manager_give_up_on_a_live_lease_and_the_next_restart_recovers(self):
        # The same restart as above, on the real threads `serve` starts (start_operations), with
        # FACTORY_MANAGER_LEASE_WAIT_SECONDS=0: a lease that is still held means "not now".
        self.write_config()
        first = self.start_manager()
        self.plan(first)
        self.start_run(first)
        self.crash_mid_build(first, 1)
        first.discard()
        tree, state = self.tree(self.data_dir), self.state_path.read_bytes()

        second = self.start_manager("threads", lease_wait_seconds=0.0)
        recovery = second.manager.operations.threads[0]
        recovery.join(BOUND)
        self.assertFalse(recovery.is_alive())
        self.assertEqual(
            second.err.getvalue().splitlines(),
            [f"factory-manager: restart repo={REPO} lease busy after 0s; recovery skipped"],
        )
        self.assertEqual((second.manager.results, second.spawner.calls, second.lines("restart")), ([], [], []))
        self.assertEqual(self.state_path.read_bytes(), state)
        self.assertEqual(self.tree(self.data_dir), tree)
        # The manager itself is up: it answers, and the dead run is shown as the dead run it is.
        [row] = self.status(second)["jobs"]
        self.assertEqual((row["status"], row["active_worker"]), ("running", False))
        second.discard()

        # The next restart finds the lease expired and recovers, without starting anything.
        self.blobs.clock.advance(LEASE_SECONDS + 1)
        third = self.start_manager("threads", lease_wait_seconds=0.0)
        recovery = third.manager.operations.threads[0]
        recovery.join(BOUND)
        self.assertFalse(recovery.is_alive())
        [line] = third.lines("restart")
        self.assertRegex(line, rf"^restart repo={REPO} run={RUN} interrupted=1 .* running=0 .* recovered=1$")
        self.assertEqual((third.err.getvalue(), third.spawner.calls, third.lines("resume")), ("", [], []))
        self.assertEqual(third.commands(), ["recover", "status"])
        [row] = self.status(third)["jobs"]
        self.assertEqual(row["status"], "interrupted")
        self.assertTrue(self.assert_no_leaks())


class AutoResumeTests(OperationsIntegration):
    def test_auto_resume_once_retries_the_interrupted_job_and_the_run_completes(self):
        self.write_config('auto_resume_after_restart = "once"')
        first = self.start_manager()
        self.plan(first)
        self.start_run(first)
        self.crash_mid_build(first, 1)
        first.discard()
        self.blobs.clock.advance(LEASE_SECONDS + 1)
        self.release("go")  # the build the manager restarts will not block

        # The real operations threads, as `serve` starts them: nothing for the operator to do.
        second = self.start_manager("threads")
        resumed = rf"^resume repo={REPO} run={RUN} retried=1 action=run pid=(\d+)$"
        self.wait_until(lambda: second.lines("resume"), "the automatic resume")
        [line] = second.lines("resume")
        self.assertRegex(line, resumed)
        self.assertEqual(self.wait_child_exit(second, "run")["returncode"], 0, self.diagnostics())
        self.assertEqual(self.wait_child_exit(second, "run")["pid"], int(re.match(resumed, line)[1]))
        self.assertEqual(second.spawner.commands(), ["run"])  # one run, started by the manager itself
        self.assertEqual(second.commands()[:3], ["recover", "status", "retry"])
        [row] = self.status(second)["jobs"]
        self.assertEqual((row["status"], row["pr"], row["attempt"]), ("pr_ready", PR_URL, 2))
        self.assertEqual(self.job()["retry_history"][-1]["reason"], RESUME_REASON)
        self.assertTrue((self.run_dir / RESUME_MARKER).is_file())
        self.assertEqual(len(self.marker_pids("build.started")), 2)
        self.read_logs("finished")

        # A second restart finds a finished run: nothing to recover, nothing to resume.
        second.discard()
        third = self.start_manager("threads")
        recovery = third.manager.operations.threads[0]
        recovery.join(BOUND)
        self.assertFalse(recovery.is_alive())
        [line] = third.lines("restart")
        self.assertRegex(line, rf"^restart repo={REPO} run={RUN} interrupted=0 .* pr_ready=1 .* recovered=0$")
        self.assertEqual((third.lines("resume"), third.spawner.calls), ([], []))
        self.assertEqual(third.commands(), ["recover", "status"])
        self.assertTrue(self.assert_no_leaks())

    def test_a_run_directory_is_resumed_automatically_only_once_however_often_the_manager_restarts(self):
        self.write_config('auto_resume_after_restart = "once"')
        first = self.start_manager()
        self.plan(first)
        self.start_run(first)
        self.crash_mid_build(first, 1)
        first.discard()
        self.blobs.clock.advance(LEASE_SECONDS + 1)

        # Restart 1 resumes the run; the resumed build blocks again (no `go` yet) and is killed too.
        second = self.start_manager("threads")
        self.wait_until(lambda: second.lines("resume repo="), "the automatic resume")
        [line] = second.lines("resume")
        self.assertRegex(line, rf"^resume repo={REPO} run={RUN} retried=1 action=run pid=\d+$")
        self.assertEqual(second.spawner.commands(), ["run"])
        marker = (self.run_dir / RESUME_MARKER).read_bytes()
        self.crash_mid_build(second, 2)
        second.discard()
        self.blobs.clock.advance(LEASE_SECONDS + 1)

        # Restart 2 recovers the job (still interrupted) but must not resume it again.
        third = self.start_manager("threads")
        recovery = third.manager.operations.threads[0]
        recovery.join(BOUND)
        self.assertFalse(recovery.is_alive())
        [line] = third.lines("restart")
        self.assertRegex(line, rf"^restart repo={REPO} run={RUN} interrupted=1 .* recovered=1$")
        self.assertEqual(third.lines("resume"), [f"resume repo={REPO} run={RUN} skipped=already-resumed"])
        self.assertEqual(third.spawner.calls, [])
        self.assertEqual(third.manager.children.running(), [])
        self.assertEqual(third.commands(), ["recover", "status"])  # no retry either
        self.assertEqual((self.run_dir / RESUME_MARKER).read_bytes(), marker)
        [row] = self.status(third)["jobs"]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(len(self.marker_pids("build.started")), 2)

        # What is left is the operator's decision, as without the setting.
        self.release("go")
        status, payload = third.call("POST", f"{BASE}/retry", {"job": JOB, "reason": "killed twice"})
        self.assertEqual((status, payload["returncode"]), (200, 0), payload)
        self.start_run(third)
        self.assertEqual(self.wait_child_exit(third, "run")["returncode"], 0, self.diagnostics())
        [row] = self.status(third)["jobs"]
        self.assertEqual((row["status"], row["attempt"]), ("pr_ready", 3))
        self.read_logs("finished")
        self.assertTrue(self.assert_no_leaks())


class AbandonedRunTests(OperationsIntegration):
    def test_a_killed_run_that_nobody_touches_is_purged_as_abandoned_after_abandon_after(self):
        self.write_config('abandon_after = "36h"')
        first = self.start_manager()
        self.plan(first)
        self.start_run(first)
        self.crash_mid_build(first, 1)
        first.discard()
        self.blobs.clock.advance(LEASE_SECONDS + 1)
        second = self.start_manager("hooks")
        second.ops.recover_all()
        [row] = self.status(second)["jobs"]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(len(self.clone_worktrees()), 2)  # the planner's and the build's, both retained

        def janitor(hours_from_now):
            def clock():
                return time.time() + hours_from_now * 3600

            return Operations(second.manager, clock=clock, wait=second.wait, lease_poll=1.0)

        # 35 hours of silence is not enough for a 36 hour limit ...
        mark = len(second.manager.results)
        janitor(35).sweep()
        self.assertEqual(second.commands(mark), ["status"])
        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual(second.lines("purge"), [])
        # ... 37 are: the run, its worktrees and branches go, then the clone, as it was the last run.
        mark = len(second.manager.results)
        janitor(37).sweep()
        self.assertEqual(second.commands(mark), ["status", "purge", "purge-clone"])
        self.assertEqual(
            second.lines("purge"),
            [f"purge repo={REPO} run={RUN} reason=abandoned", f"purge-clone repo={REPO} removed=yes"],
        )
        self.assertFalse(self.run_dir.exists())
        self.assertFalse(self.clone.exists())
        self.assertTrue(self.assert_no_leaks())


class CostGuardrailTests(OperationsIntegration):
    def test_the_manager_drains_a_run_over_its_budget_once_and_the_run_keeps_its_checkpoints(self):
        self.write_config("run_budget_usd = 0.001")
        self.release("go")  # the build is not held; the review is
        self.release("hold-review")
        manager = self.start_manager("hooks")
        self.plan(manager)
        self.start_run(manager)
        self.wait_for_marker("review.started", 1)  # the build is paid for (0.0125), the review is running
        [row] = self.status(manager)["jobs"]
        self.assertEqual(row["status"], "running")
        self.assertGreater(self.status(manager)["cost_usd"], 0.001)

        manager.ops.check_budgets()
        manager.ops.check_budgets()  # a second pass does not drain again
        self.assertEqual(manager.commands().count("drain"), 1)
        [line] = manager.lines("budget")
        self.assertRegex(line, rf"^budget repo={REPO} run={RUN} cost=\d+\.\d\d limit=0\.00 action=drain$")
        self.assertTrue((self.run_dir / BUDGET_MARKER).is_file())
        control = json.loads((self.run_dir / "supervisor-control.json").read_text())["current"]
        self.assertTrue(control["drain_requested_at"])

        # The review in flight finishes; nothing new starts; the supervisor leaves.
        self.release("go-review")
        self.assertEqual(self.wait_child_exit(manager, "run")["returncode"], 0, self.diagnostics())
        self.read_logs("drained")
        self.assertIn("Supervisor drained; checkpoints retained.", self.logs["drained/supervisor.log"])
        report = self.status(manager)
        [row] = report["jobs"]
        self.assertTrue(row["status"].startswith("waiting_"), row["status"])
        self.assertIsNone(row["pr"])
        self.assertEqual((row["attempt"], row["review_passed"], row["review_matches_head"]), (1, True, True))
        self.assertEqual(report["supervisor"]["status"], "drained")
        job = self.job()
        self.assertTrue(job["commit"])  # the build's commit, kept
        self.assertEqual(job["reviewed_commit"], job["commit"])
        self.assertTrue(Path(job["worktree"]).is_dir())
        self.assertEqual(self.branches(self.clone, f"factory/{RUN}/{JOB}"), [f"factory/{RUN}/{JOB}"])
        self.assertEqual([call for call in self.gh_log() if call["args"][:2] == ["pr", "create"]], [])

        # Running it again continues from the checkpoints: no second build, no new attempt.
        self.start_run(manager)
        self.assertEqual(self.wait_child_exit(manager, "run")["returncode"], 0, self.diagnostics())
        [row] = self.status(manager)["jobs"]
        self.assertEqual((row["status"], row["pr"], row["attempt"]), ("pr_ready", PR_URL, 1))
        self.assertEqual(len(self.marker_pids("build.started")), 1)
        self.assertEqual(manager.commands().count("drain"), 1)
        self.read_logs("finished")
        self.assert_text_travelled()
        self.assertTrue(self.assert_no_leaks())


if __name__ == "__main__":
    unittest.main()
