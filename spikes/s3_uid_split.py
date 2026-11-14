#!/usr/bin/env python3
"""Spike S3: can the supervisor start workers as another UID, with only the
capabilities it needs, and can the worker not see the supervisor's secrets?

Run it inside the Factory container on ACA as the unprivileged `factory` user
(the image entrypoint drops root, makes `factory-work` (10010) a supplementary
group of the supervisor only, and keeps the ambient capabilities):

    python3 s3_uid_split.py [--worker-uid 10002] [--worker-gid 10010]
                            [--expect-caps setuid,setgid,kill]

Exit 0 = every check passed. The report is one JSON object on stdout.

What it proves (spike S3, plus the kill check below):
  * Popen(user=, group=, extra_groups=[]) works with only ambient CAP_SETUID and
    CAP_SETGID (the supervisor has no other way to change UID);
  * the child has the worker identity, no supplementary groups and NO capabilities:
    the kernel keeps capabilities across a setuid between two non-zero UIDs and the
    ambient set survives exec, so the worker must go through factory.execnocaps (what
    run_agent does). The worker also tries os.setuid()/os.setgid() back to the
    supervisor and to root: every attempt must fail with EPERM;
  * the child cannot read the supervisor's /proc/<pid>/environ (the App key and
    the Anthropic key live there) nor any other readable /proc/*/environ;
  * the child cannot list the supervisor's private directory (0750) but can use a
    setgid group-shared directory, and the supervisor can read what it wrote;
  * the supervisor can SIGKILL the worker's whole process group. kill(2) needs
    CAP_KILL when the UIDs differ, so this is why the expected ambient set includes
    `kill`. If this check fails with EPERM, the image entrypoint is missing cap_kill.

`--mechanism none` starts the worker WITHOUT the wrapper. It is expected to FAIL the
capability and escalation checks (that is the baseline that shows the test can see
the problem); `--mechanism factory` (the default) must PASS.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CAPS = {
    "chown": 0, "dac_override": 1, "dac_read_search": 2, "fowner": 3, "fsetid": 4,
    "kill": 5, "setgid": 6, "setuid": 7, "setpcap": 8, "net_bind_service": 10,
    "net_admin": 12, "net_raw": 13, "sys_chroot": 18, "sys_ptrace": 19,
    "sys_admin": 21, "mknod": 27, "audit_write": 29, "setfcap": 31,
}
CANARY = "s3-canary-value-not-a-secret"

# Runs as the worker. Prints one JSON object. Arguments: parent pid, shared dir, private dir.
CHILD = r"""
import json, os, sys
ppid, shared, private = int(sys.argv[1]), sys.argv[2], sys.argv[3]
sup_uid, sup_gid = int(sys.argv[4]), int(sys.argv[5])
out = {"uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "groups": os.getgroups()}
status = {}
for line in open("/proc/self/status"):
    key, _, value = line.partition(":")
    if key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"):
        status[key] = value.strip()
out["status"] = status
escalation = {}
attempts = [
    ("setuid_to_supervisor", os.setuid, sup_uid),
    ("setuid_to_root", os.setuid, 0),
    ("setgid_to_root", os.setgid, 0),
]
if sup_gid != os.getgid():  # setgid to the worker's own group would trivially succeed
    attempts.append(("setgid_to_supervisor", os.setgid, sup_gid))
for label, call, value in attempts:
    try:
        call(value)
        escalation[label] = "SUCCEEDED"
    except OSError as error:
        escalation[label] = type(error).__name__
out["escalation"] = escalation
try:
    open(f"/proc/{ppid}/environ", "rb").read(1)
    out["parent_environ_readable"] = True
except OSError as error:
    out["parent_environ_readable"] = False
    out["parent_environ_error"] = type(error).__name__
leaks = 0
for name in os.listdir("/proc"):
    if name.isdigit():
        try:
            if b"s3-canary-value" in open(f"/proc/{name}/environ", "rb").read():
                leaks += 1
        except OSError:
            pass
out["canary_visible_in_other_environ"] = leaks
try:
    os.listdir(private)
    out["private_dir_listable"] = True
except OSError as error:
    out["private_dir_listable"] = False
    out["private_dir_error"] = type(error).__name__
try:
    with open(os.path.join(shared, "from-worker.txt"), "w") as handle:
        handle.write("worker wrote this")
    out["shared_dir_writable"] = True
except OSError as error:
    out["shared_dir_writable"] = False
    out["shared_dir_error"] = type(error).__name__
print(json.dumps(out))
sys.stdout.flush()
import time
time.sleep(20)
"""


def own_status():
    result = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in ("Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"):
            result[key] = value.strip()
    return result


def caps_mask(names):
    mask = 0
    for name in names:
        if name not in CAPS:
            raise SystemExit(f"unknown capability {name!r}")
        mask |= 1 << CAPS[name]
    return mask


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--worker-uid", type=int, default=10002)
    parser.add_argument("--worker-gid", type=int, default=10010)
    parser.add_argument("--expect-caps", default="setuid,setgid,kill")
    parser.add_argument("--mechanism", choices=("factory", "none"), default="factory")
    args = parser.parse_args()

    expected = caps_mask([name for name in args.expect_caps.split(",") if name])
    report = {"supervisor": own_status(), "expected_caps": args.expect_caps}
    ambient = int(report["supervisor"]["CapAmb"], 16)
    report["ambient_matches"] = ambient == expected
    report["supervisor_not_root"] = os.getuid() != 0
    os.environ["S3_CANARY"] = CANARY  # present in this process's /proc/<pid>/environ

    work = Path(tempfile.mkdtemp(prefix="s3-"))
    shared = work / "shared"
    private = work / "private"
    shared.mkdir(mode=0o770)
    private.mkdir(mode=0o750)  # owner = supervisor, group = the supervisor's PRIMARY group
    work.chmod(0o751)  # traversable, so a failure below is about the directory itself
    try:
        os.chown(shared, -1, args.worker_gid)
        os.chmod(shared, 0o2770)
    except OSError as error:
        report["shared_dir_setup_error"] = f"{type(error).__name__}: {error}"

    command = [sys.executable, "-c", CHILD, str(os.getpid()), str(shared), str(private), str(os.getuid()), str(os.getgid())]
    if args.mechanism == "factory":
        from factory.agent import drop_capabilities_first

        command = drop_capabilities_first(command)
    child = subprocess.Popen(
        command,
        user=args.worker_uid,
        group=args.worker_gid,
        extra_groups=[],
        umask=0o002,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
        text=True,
    )
    try:
        line = child.stdout.readline()
        worker = json.loads(line) if line else {}
        report["worker"] = worker
        status = worker.get("status", {})
        report["worker_identity_ok"] = (
            worker.get("uid") == args.worker_uid
            and worker.get("euid") == args.worker_uid
            and worker.get("gid") == args.worker_gid
            and worker.get("groups") == []
        )
        report["worker_has_no_caps"] = all(int(status.get(key, "1"), 16) == 0 for key in ("CapPrm", "CapEff", "CapAmb"))
        report["worker_cannot_change_identity"] = bool(worker.get("escalation")) and all(
            outcome == "PermissionError" for outcome in worker["escalation"].values()
        )
        report["worker_cannot_read_supervisor_environ"] = (
            worker.get("parent_environ_readable") is False and worker.get("canary_visible_in_other_environ") == 0
        )
        report["worker_cannot_list_private_dir"] = worker.get("private_dir_listable") is False
        written = shared / "from-worker.txt"
        try:
            report["supervisor_reads_worker_file"] = written.read_text() == "worker wrote this"
        except OSError as error:
            report["supervisor_reads_worker_file"] = False
            report["supervisor_read_error"] = type(error).__name__
        report["worker_writes_shared_dir"] = worker.get("shared_dir_writable") is True

        try:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=30)
            report["supervisor_can_kill_worker_group"] = True
        except OSError as error:
            report["supervisor_can_kill_worker_group"] = False
            report["kill_error"] = f"{type(error).__name__}: {error}"
        except subprocess.TimeoutExpired:
            report["supervisor_can_kill_worker_group"] = False
            report["kill_error"] = "worker did not exit after SIGKILL"
        time.sleep(0.2)
    finally:
        if child.poll() is None:
            try:
                child.kill()
                child.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                # The probe worker exits by itself after 20 seconds.
                report["worker_left_running"] = f"{type(error).__name__}"
        for path in (shared / "from-worker.txt",):
            path.unlink(missing_ok=True)
        leftovers = []
        for path in (shared, private, work):
            try:
                path.rmdir()
            except OSError as error:
                leftovers.append(f"{path}: {type(error).__name__}")
        if leftovers:
            report["cleanup_left_behind"] = leftovers

    checks = (
        "ambient_matches",
        "supervisor_not_root",
        "worker_identity_ok",
        "worker_has_no_caps",
        "worker_cannot_change_identity",
        "worker_cannot_read_supervisor_environ",
        "worker_cannot_list_private_dir",
        "worker_writes_shared_dir",
        "supervisor_reads_worker_file",
        "supervisor_can_kill_worker_group",
    )
    report["failed"] = [name for name in checks if not report.get(name)]
    report["pass"] = not report["failed"]
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
