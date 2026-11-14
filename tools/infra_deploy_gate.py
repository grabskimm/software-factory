#!/usr/bin/env python3
"""Pause, drain and resume the Factory manager around a Terraform apply (infra/README.md, "Safe deployments").

An optional operator script: run it from the operator's own pipeline (or by hand) after the apply was approved
and before `terraform apply`, and again after the rollout check:

    infra_deploy_gate.py pause    POST /trigger-pause {"paused": true}; refuse to go on unless it is set
    infra_deploy_gate.py drain    POST .../drain for every run a supervisor is active for; the runs that
                                  accepted it are remembered in GATE_STATE_FILE
    infra_deploy_gate.py wait     poll GET /repos until no supervisor is active and no worker pod is
                                  running or unreadable, at most GATE_WAIT_SECONDS (default 1800); on
                                  timeout exit 1 and name the active runs (and the pods) in the step
                                  summary, so the pipeline fails before applying
    infra_deploy_gate.py resume   POST /trigger-pause {"paused": false}, retried for GATE_RESUME_SECONDS
                                  (default 300); if the pause cannot be cleared, exit 1 and say so loudly.
                                  Then POST .../run for every run this script drained, so it continues on the
                                  new pod (a drained run is not interrupted, so nothing else resumes it)

Environment: FACTORY_MANAGER_URL (``manager_url`` output), FACTORY_MANAGER_AUDIENCE (a value of the
manager's FACTORY_ENTRA_AUDIENCES), GITHUB_STEP_SUMMARY (optional), GATE_STATE_FILE (optional; the
drained runs, default ``$RUNNER_TEMP/factory-drained.json``, else the system temporary directory).

The bearer token is the apply identity's own Entra token for that audience, fetched with
``az account get-access-token`` from the Azure session the pipeline opened (OIDC, so no secret). It lives
in this process only: it is sent as the Authorization header of the manager requests and is never
printed, written to a file, put in the environment of another process or echoed in an error. HTTP
errors are reported by status code only, never by response body. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_SECONDS = 30
TOKEN_SECONDS = 120
SAFE = re.compile(r"[^A-Za-z0-9._/-]")  # repository and run names are validated by the manager; belt and braces


class GateError(Exception):
    """A failure with a message that is safe to print (it never carries a token or a response body)."""


class Refused(GateError):
    """A condition that ends the wait at once instead of being retried until the deadline."""


def fetch_token(audience: str) -> str:
    """The Entra access token of the logged-in identity for ``audience``; never printed."""
    try:
        done = subprocess.run(
            ["az", "account", "get-access-token", "--resource", audience, "--query", "accessToken", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=TOKEN_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f"could not run az to get a manager token ({type(error).__name__})") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not token:
        raise Refused(
            "az could not issue a token for the manager audience: is the apply identity assigned the "
            "Factory.Operator app role on the manager app registration (infra/README.md)?"
        )
    return token


class Manager:
    """The three manager calls this script needs, over HTTPS with a fresh token per request batch."""

    def __init__(self, base_url: str, audience: str, token=fetch_token, opener=urllib.request.urlopen):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.query or parsed.fragment:
            raise GateError("FACTORY_MANAGER_URL must be an http(s) URL")
        self.base = base_url.rstrip("/")
        self.audience = audience
        self._token = token
        self._open = opener

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        """(status, parsed JSON or None). A transport failure raises GateError; an HTTP error status is returned."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self._token(self.audience))
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._open(request, timeout=REQUEST_SECONDS) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, b""
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise GateError(f"{method} {path} failed ({type(error).__name__})") from None
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, None

    def set_pause(self, paused: bool) -> None:
        status, payload = self.call("POST", "/trigger-pause", {"paused": paused})
        if status != 200 or not isinstance(payload, dict) or payload.get("trigger_paused") is not paused:
            raise GateError(f"POST /trigger-pause paused={str(paused).lower()} answered {status}")

    def snapshot(self) -> tuple[bool, list]:
        """(trigger paused, what is still active) from GET /repos.

        What is active is a list of ``(repo, kind, run)``: a supervisor of the run or plan kind, and, when the manager
        reports its worker pods (``worker_pods``, only a manager with the pod backend wired in does), one entry of kind
        ``pods`` while a pod is running or cannot be read (``pods_entry``). A manager without the key reports no pods,
        which is what an older image says: the gate must not wait for what it cannot see.
        """
        status, payload = self.call("GET", "/repos")
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("repos"), list):
            raise GateError(f"GET /repos answered {status}")
        active = []
        for entry in payload["repos"]:
            supervisor = entry.get("supervisor") if isinstance(entry, dict) else None
            if isinstance(supervisor, dict) and supervisor.get("running", True):
                active.append((str(entry.get("repo")), str(supervisor.get("kind")), str(supervisor.get("run"))))
        if "worker_pods" in payload:
            busy = pods_entry(payload["worker_pods"])
            if busy is not None:
                active.append(busy)
        return payload.get("trigger_paused") is True, active

    def drain(self, repo: str, run: str) -> int:
        return self.run_action(repo, run, "drain")

    def start_run(self, repo: str, run: str) -> int:
        return self.run_action(repo, run, "run")

    def run_action(self, repo: str, run: str, action: str) -> int:
        owner, _, name = repo.partition("/")
        quote = urllib.parse.quote
        return self.call("POST", f"/repos/{quote(owner)}/{quote(name)}/runs/{quote(run)}/{action}", {})[0]


def say(text: str) -> None:
    print(text, flush=True)


def summary(lines: list) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def pods_entry(census: object) -> tuple | None:
    """The ``(repo, "pods", detail)`` entry for the manager's pod census, or None when no pod needs waiting for.

    ``census`` is ``{"live": n, "final": m, "unknown": bool}``. A pod that is running (``live``) is a session a rollout
    would cut off, and one that cannot be read is treated the same: only a census that says ``unknown`` is false and
    ``live`` is exactly 0 is idle. Anything else (a malformed value, a negative or boolean count) is busy and
    unreadable, never idle.
    """
    live = census.get("live") if isinstance(census, dict) else None
    if isinstance(census, dict) and census.get("unknown") is False and type(live) is int and live >= 0:
        return None if live == 0 else ("worker-pods", "pods", f"{live}-running")
    return ("worker-pods", "pods", "unreadable")


def label(entry: tuple) -> str:
    repo, kind, run = (SAFE.sub("?", part) for part in entry)
    if kind == "pods":
        return f"worker pods ({run})"
    return f"{repo} run {run} ({kind})"


def pause(manager: Manager, **_: object) -> None:
    manager.set_pause(True)
    say("trigger paused: the manager starts no new plan or run; runs in progress are untouched")


def state_path(environ=None) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("GATE_STATE_FILE", "").strip()
    return explicit or os.path.join(env.get("RUNNER_TEMP") or tempfile.gettempdir(), "factory-drained.json")


def load_drained(state: str | None) -> list:
    """The (repo, run) pairs this script drained, oldest first; empty when nothing was recorded or the file is unreadable."""
    if not state:
        return []
    try:
        with open(state, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    pairs = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item):
            pairs.append((item[0], item[1]))
    return pairs


def record_drained(state: str | None, repo: str, run: str) -> None:
    if not state or (repo, run) in load_drained(state):
        return
    pairs = [*load_drained(state), (repo, run)]
    temporary = f"{state}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump([list(pair) for pair in pairs], handle)
    os.replace(temporary, state)


def ask_drain(manager: Manager, active: list, asked: set, state: str | None = None) -> None:
    """Ask each active ``run`` supervisor not asked yet to drain. A ``plan`` ends by itself."""
    for entry in active:
        repo, kind, run = entry
        if kind != "run" or entry in asked:
            continue
        status = manager.drain(repo, run)
        asked.add(entry)
        if status == 200:
            record_drained(state, repo, run)  # the resume step starts it again on the new pod
        # 422: the supervisor ended (or has no drain record) between the listing and the request; the wait re-reads.
        say(f"drain requested for {label(entry)}: HTTP {status}")


def drain(manager: Manager, state: str | None = None, **_: object) -> None:
    _, active = manager.snapshot()
    if not active:
        say("no active supervisor: nothing to drain")
    ask_drain(manager, active, set(), state)


def wait(
    manager: Manager,
    timeout: float = 1800.0,
    poll: float = 15.0,
    clock=time.monotonic,
    sleep=time.sleep,
    state: str | None = None,
    **_: object,
) -> None:
    """Poll until no supervisor is active; drain one that appeared meanwhile; fail at the deadline."""
    deadline = clock() + timeout
    asked: set = set()
    while True:
        active: list = []
        try:
            paused, active = manager.snapshot()
            error = None
            if not paused:
                raise Refused("the trigger is not paused any more (someone cleared it); refusing to go on")
            if not active:
                say("no supervisor is active: safe to apply")
                return
            ask_drain(manager, active, asked, state)
        except Refused:
            raise
        except GateError as problem:
            error = str(problem)  # a restarting ingress must not end the wait early, only the deadline does
        if clock() >= deadline:
            names = [label(entry) for entry in active]
            lines = ["### Factory drain timed out: nothing was applied", ""]
            if error is not None:
                lines.append(f"The manager could not be read at the deadline ({error}).")
            else:
                lines.append(f"Supervisors (or worker pods) still active after {timeout / 60:g} minutes:")
                lines.extend(f"- `{name}`" for name in names)
            lines += ["", "The trigger pause is cleared again, and the drained runs are started again, by the last step of the job."]
            summary(lines)
            detail = error or "still active: " + "; ".join(names)
            raise GateError(f"drain did not finish within {timeout / 60:g} minutes ({detail}); nothing was applied")
        if error is None:
            say(f"waiting for {len(active)} supervisor(s): " + "; ".join(label(entry) for entry in active))
        sleep(min(poll, max(0.0, deadline - clock())))


def restart_drained(manager: Manager, state: str | None, deadline: float, poll: float, clock, sleep) -> None:
    """``run`` again every run this script drained: it was drained, not interrupted, so nothing else continues it.

    Best effort, retried until the deadline (the manager may still be coming back). A run that cannot be
    started is named in the summary: it needs ``run`` from an Operator; the script does not fail for it.
    """
    pending = load_drained(state)
    failed = []
    for repo, run in pending:
        entry = (repo, "run", run)
        while True:
            try:
                status = manager.start_run(repo, run)
            except GateError:
                status = 0
            if status in (200, 202):
                say(f"run started again for {label(entry)}: HTTP {status}")
                break
            if status in (404, 409, 422) or clock() >= deadline:
                # 409: a supervisor is active already (it did not finish draining, or a person started it); 404/422: nothing to run
                say(f"::warning::run not started again for {label(entry)}: HTTP {status}")
                failed.append(entry)
                break
            sleep(min(poll, max(0.0, deadline - clock())))
    if failed:
        summary(
            ["### Drained Factory runs that were not started again", ""]
            + [f"- `{label(entry)}`: if it is not running, start it with `POST .../run`" for entry in failed]
        )
    if pending and state:
        try:
            os.unlink(state)
        except OSError:
            pass


def resume(
    manager: Manager,
    timeout: float = 300.0,
    poll: float = 10.0,
    clock=time.monotonic,
    sleep=time.sleep,
    state: str | None = None,
    **_: object,
) -> None:
    """Clear the pause, retrying while the manager restarts; if that fails the pause is LEFT SET and said so.

    Once the pause is clear, the runs this script drained are started again.
    """
    deadline = clock() + timeout
    while True:
        try:
            manager.set_pause(False)
            say("trigger resumed: the manager starts plans and runs again")
            restart_drained(manager, state, deadline, poll, clock, sleep)
            return
        except GateError as problem:
            if clock() >= deadline:
                summary(
                    [
                        "### Factory trigger pause is STILL SET",
                        "",
                        f"The pause could not be cleared ({problem}). The manager starts no plan or run until an Operator "
                        'clears it: `POST /trigger-pause` with `{"paused": false}` (docs/operations.md, "Safe deployments").',
                    ]
                )
                raise GateError(f"the trigger pause is STILL SET and must be cleared by an Operator ({problem})") from None
        sleep(min(poll, max(0.0, deadline - clock())))


COMMANDS = {"pause": pause, "drain": drain, "wait": wait, "resume": resume}


def number(name: str, default: float) -> float:
    text = os.environ.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise GateError(f"{name} must be a number of seconds") from None
    if not 0 < value <= 86_400:
        raise GateError(f"{name} must be between 0 and 86400 seconds")
    return value


def main(argv: list | None = None, environ=None, manager: Manager | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in COMMANDS:
        print(f"usage: infra_deploy_gate.py {'|'.join(COMMANDS)}", file=sys.stderr)
        return 2
    env = os.environ if environ is None else environ
    try:
        if manager is None:
            url, audience = env.get("FACTORY_MANAGER_URL", "").strip(), env.get("FACTORY_MANAGER_AUDIENCE", "").strip()
            if not url or not audience:
                raise GateError("FACTORY_MANAGER_URL and FACTORY_MANAGER_AUDIENCE must both be set")
            manager = Manager(url, audience)
        options: dict = {"state": state_path(env)}
        if args[0] == "wait":
            options |= {"timeout": number("GATE_WAIT_SECONDS", 1800.0), "poll": number("GATE_POLL_SECONDS", 15.0)}
        if args[0] == "resume":
            options |= {"timeout": number("GATE_RESUME_SECONDS", 300.0), "poll": number("GATE_POLL_SECONDS", 10.0)}
        COMMANDS[args[0]](manager, **options)
    except GateError as error:
        say(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
