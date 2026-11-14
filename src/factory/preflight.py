"""Probe the actual worker sandbox before assigning environment-dependent work."""

from datetime import datetime, timezone

from .agent import AgentError, run_agent, sibling
from .blockers import record_capabilities
from .phases import domain_allows, normalize_domains, probe_host
from .podexecution import agent_kwargs
from .state import atomic_json

CAPABILITIES = {
    "workspace_write",
    "shared_git_write",
    "loopback_listener",
    "network_download",
}


def probe_worker(runner, job_id, phase, worktree, timeout=120):
    """Use the same sandbox flags as execution; never infer provider/host access."""
    if phase not in {"build", "verify"}:
        raise ValueError("preflight phase must be build or verify")
    options = runner.options
    host = probe_host(options.probe_url)
    if phase == "verify" and not domain_allows(
        normalize_domains(options.verify_domains), host
    ):
        # Otherwise the probe would report "unavailable" for a reason that says
        # nothing about the verify sandbox.
        raise ValueError(
            f"probe URL host {host} is not on the verify allowlist; "
            "add it with --verify-domain"
        )
    tested_at = datetime.now(timezone.utc).isoformat()
    prefix = (
        runner.directory
        / job_id
        / f"preflight-{phase}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    )
    script = """import json, pathlib, socket, subprocess, tempfile, urllib.request
PROBE_URL = __PROBE_URL__
observed = {}
def check(name, operation):
    try:
        operation()
        observed[name] = {"status": "available", "evidence": "Probe completed successfully"}
    except Exception as error:
        observed[name] = {"status": "unavailable", "evidence": type(error).__name__ + ": " + str(error)}
def write_probe(directory):
    with tempfile.TemporaryFile(dir=directory, prefix="factory-preflight-") as handle:
        handle.write(b"probe")
        handle.flush()
def loopback():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
def network():
    with urllib.request.urlopen(PROBE_URL, timeout=5) as response:
        response.read(1)
check("workspace_write", lambda: write_probe(pathlib.Path.cwd()))
common = subprocess.check_output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip()
check("shared_git_write", lambda: write_probe(common))
check("loopback_listener", loopback)
check("network_download", network)
print(json.dumps(observed))
""".replace("__PROBE_URL__", repr(options.probe_url))
    prompt = (
        "Run this capability probe exactly once using your Bash tool in the assigned worktree. "
        "Do not edit source, install packages, start services, inspect credentials, or dump environment variables. "
        "The script uses temporary files that clean themselves up, an ephemeral loopback listener, "
        "and one HTTPS download request. Do not retry denied operations. Return ONLY the JSON it prints. "
        "If Bash itself cannot run, report every capability as unconfirmed with the actual denial as evidence. "
        "These probes measure only the four listed capabilities; do not infer any other access or permission from them.\n\n"
        + script
    )
    try:
        result, usage = run_agent(
            prompt,
            worktree,
            prefix,
            "preflight",
            timeout,
            on_start=lambda started: runner.state.record_started(job_id, started),
            options=options,
            target=phase,
            guidance=runner.guidance(job_id, worktree),
            user=runner.cfg.worker_uid,
            group=runner.cfg.worker_gid,
            **agent_kwargs(
                runner.cfg.execution,
                job_id=job_id,
                attempt=runner.state.data["jobs"][job_id].get("attempt", 0),
                base=runner.state.data["jobs"][job_id].get("base"),
                spent_usd=runner.run_cost,
                budget_usd=runner.cfg.run_budget_usd,
            ),
        )
    except AgentError as error:
        if error.usage:
            previous = runner.state.data["jobs"][job_id].get("usage", [])
            runner.state.update(job_id, usage=previous + [error.usage])
        raise
    finally:
        runner.state.end_session(job_id)  # a pod that is not known to be gone stays recorded
    if not isinstance(result, dict) or set(result) != CAPABILITIES:
        raise ValueError("preflight returned an invalid capability set")
    environment = f"{job_id}:{phase}:{worktree}"
    records = []
    for name, value in result.items():
        if (
            not isinstance(value, dict)
            or value.get("status") not in {"available", "unavailable", "unconfirmed"}
            or not isinstance(value.get("evidence"), str)
            or not value["evidence"].strip()
        ):
            raise ValueError(f"invalid preflight observation: {name}")
        records.append(
            {
                "id": name,
                "environment": environment,
                "status": value["status"],
                "tested_at": tested_at,
                "evidence": [value["evidence"], str(sibling(prefix, ".jsonl"))],
            }
        )
    atomic_json(sibling(prefix, ".json"), result)
    saved = record_capabilities(runner.state, records)
    previous = runner.state.data["jobs"][job_id].get("usage", [])
    runner.state.update(
        job_id, usage=previous + [usage], preflight_environment=environment
    )
    return {"environment": environment, "capabilities": saved}
