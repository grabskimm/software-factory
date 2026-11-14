#!/usr/bin/env python3
"""Probes that run INSIDE a pod of the mirror (slice 21 of the pod backend, docs/pod-backend-runbook.md, Part G).

The runbook has a few claims that only a process inside the real pod can settle: who may talk to the launcher's socket, who may
list the spool, what the owner and mode of the Secret volume's files are and whether the worker identity can open them, which names
the pod's environment has, and what the API server really answers the launcher's client. The Runner copies nothing: this file is
fed to an interpreter that is already in the image, `kubectl exec -i POD -c CONTAINER -- python3 - MODE ARGS < tools/live_pod_probe.py`,
so the probe is a literal argv list for `kubectl` and the file never lands on a pod's disk.

    socket          [--expect worker|supervisor] [--as-uid U --as-gid G]   the launcher's socket directory
    spool           [--expect worker|supervisor] [--as-uid U --as-gid G]   the relay's spool
    secret-volume   [--as-uid U --as-gid G]                                the two files of the Secret volume
    environment     [--expect worker-pod]                                  the NAMES (never the values) of the environment
    network         --control-host H --api-public-host H                   the network fence, from a hand-made pod of the mirror
    statuses        [--bodies]                                             what the API server answers the launcher's client

What it can change: nothing that matters. `socket` and `spool` try to create a file with a fresh name (and bind a socket with one)
and remove it again when the attempt succeeds; whether the directory can be renamed in or unlinked from is asked with `access`, not
tried. `statuses` makes read calls and ONE server-side dry run that persists nothing.
It never reads the content of a secret file (it opens it and closes it), never prints a token, a value of the environment or a
response body (unless `--bodies` is given for `statuses`, whose bodies are Status objects and the policy's denial text), and never
runs a command it was handed. Standard library only; the paths are the contract's (``tests/test_live_pod_probe.py`` compares them
with ``factory.podcontract``, which is not importable here).

Exit status: 0 every answer was as expected, 1 one was not, 2 a usage or environment error.
"""

import argparse
import errno
import http.client
import json
import os
import socket
import ssl
import stat
import sys

SOCKET_DIR = "/run/factory"
SOCKET = "/run/factory/launcher.sock"
SPOOL = "/spool"
SECRET_DIR = "/run/factory-session"
SECRET_FILES = ("token", "anthropic-api-key")
WORKER_NS = "factory-workers"
SERVICE_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TIMEOUT = 5.0
MAX_TOKEN_BYTES = 16384
MAX_BODY_BYTES = 1 << 20
SECRET_LIKE = ("KEY", "TOKEN", "SECRET", "PASSWORD")
# A name the IMAGE ITSELF sets (the Dockerfile's ENV) that contains one of those words but holds no secret: FACTORY_TOKEN_BROKER is the command
# line of the GitHub App token broker. Every container of the image has it, a worker pod included (found by the first live run,
# docs/pod-backend-local-e2e.md), so counting it made `environment --expect worker-pod` fail on a pod that is exactly as designed.
# tests/test_live_pod_probe.py reads the Dockerfile and fails when it gains another such name that is not listed here.
IMAGE_ENV_NAMES = ("FACTORY_TOKEN_BROKER",)
# The destinations the worker's network canary must not reach (the contract's CANARY_FORBIDDEN; a host of the form $NAME is read from
# that environment variable, which a hand-made pod sets from the downward API) and the one it may (a control that proves the
# network works at all). tests/test_live_pod_probe.py compares the list with factory.podcontract.
FORBIDDEN = (
    ("the instance metadata address", "169.254.169.254", 80),
    ("the Azure host channel", "168.63.129.16", 80),
    ("the Azure host channel (wire server)", "168.63.129.16", 32526),
    ("the node's kubelet port", "$FACTORY_NODE_IP", 10250),
    ("the manager's API port", "$FACTORY_MANAGER_ADDRESS", 8080),
    ("the manager's metrics port", "$FACTORY_MANAGER_METRICS_ADDRESS", 9464),
)
API_PATH = "/api/v1/namespaces/factory/secrets"
DENIED_STATUSES = (401, 403)

REFUSED = (errno.EACCES, errno.EPERM, errno.EROFS)


class Environment(Exception):
    """The probe cannot run here (not a pod, no privilege to switch identity); exit status 2."""


def word(error):
    """The closed word for an OSError."""
    if error.errno in REFUSED:
        return "refused"
    if error.errno == errno.ENOENT:
        return "absent"
    if error.errno == errno.ECONNREFUSED:
        return "no listener"
    return "error"


def attempt(action):
    """'allowed' when the action succeeds, else the word of its error."""
    try:
        action()
    except OSError as error:
        return word(error)
    return "allowed"


def drop_identity(uid, gid):
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:
        raise PermissionError("identity")


def connect(path):
    def action():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(TIMEOUT)
            probe.connect(path)

    return attempt(action)


def create_in(directory):
    path = os.path.join(directory, f"probe-{os.getpid()}")

    def action():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.unlink(path)

    return attempt(action)


def write_access(directory):
    """Whether the directory can be written and searched, which is what renaming or unlinking in it needs. It is asked of the
    kernel (`access`) and not tried: a rename or an unlink of a file that does not exist fails with ENOENT whatever the
    permissions of the directory are, so it proves nothing."""
    if not os.path.isdir(directory):
        return "absent"
    return "allowed" if os.access(directory, os.W_OK | os.X_OK) else "refused"


def bind_in(directory):
    path = os.path.join(directory, f"probe-{os.getpid()}.sock")

    def action():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.bind(path)
        os.unlink(path)

    return attempt(action)


def judge(emit, label, got, want):
    ok = got == want
    emit(f"{label}: {got} (expected {want})" if ok else f"FAIL {label}: {got} (expected {want})")
    return 0 if ok else 1


def mode_socket(args, emit):
    supervisor = args.expect == "supervisor"
    problems = judge(emit, "connect to the launcher socket", connect(SOCKET), "allowed" if supervisor else "refused")
    for label, probe in (
        ("create a file in the socket directory", create_in),
        ("rename or unlink in the socket directory (write access)", write_access),
        ("bind a socket in the socket directory", bind_in),
    ):
        problems += judge(emit, label, probe(SOCKET_DIR), "refused")
    return problems


def mode_spool(args, emit):
    supervisor = args.expect == "supervisor"
    want = "allowed" if supervisor else "refused"
    problems = judge(emit, "list the spool", attempt(lambda: os.listdir(SPOOL)), want)
    return problems + judge(emit, "create a file in the spool", create_in(SPOOL), want)


def mode_secret_volume(args, emit):
    problems = 0
    for name in SECRET_FILES:
        try:
            info = os.stat(os.path.join(SECRET_DIR, name))
        except OSError as error:
            emit(f"FAIL secret file {name}: {word(error)}")
            problems += 1
            continue
        mode = stat.S_IMODE(info.st_mode)
        regular = stat.S_ISREG(info.st_mode)
        emit(f"secret file {name}: regular {'yes' if regular else 'no'}, mode {mode:04o}, owner {info.st_uid}, group {info.st_gid}")
        if not regular or mode & 0o077:
            emit(f"FAIL secret file {name}: it must be a regular file with no group or other permission")
            problems += 1
    if args.as_uid is not None:
        try:
            drop_identity(args.as_uid, args.as_gid)
        except (PermissionError, OSError):
            emit("FAIL cannot switch to the requested identity (this needs root or CAP_SETUID)")
            raise Environment
        for name in SECRET_FILES:
            opened = attempt(lambda name=name: os.close(os.open(os.path.join(SECRET_DIR, name), os.O_RDONLY)))
            problems += judge(emit, f"open secret file {name} as uid {args.as_uid}", opened, "refused")
    return problems


def mode_environment(args, emit):
    names = list(os.environ)
    host = "KUBERNETES_SERVICE_HOST" in os.environ
    port = "KUBERNETES_SERVICE_PORT" in os.environ
    azure = sum(1 for name in names if name.startswith("AZURE_"))
    secret_like = sum(1 for name in names if name not in IMAGE_ENV_NAMES and any(part in name.upper() for part in SECRET_LIKE))
    emit(f"environment: {len(names)} names, KUBERNETES_SERVICE_HOST {'present' if host else 'absent'}, KUBERNETES_SERVICE_PORT {'present' if port else 'absent'}")
    emit(f"environment: {azure} names start with AZURE_, {secret_like} names contain KEY, TOKEN, SECRET or PASSWORD")
    defined = [name for name in IMAGE_ENV_NAMES if name in os.environ]
    if defined:
        emit(f"environment: {len(defined)} image-defined name not counted ({', '.join(defined)}: a command line, not a secret)")
    if args.expect != "worker-pod":
        return 0
    problems = 0
    for ok, text in ((host and port, "the API server address is not in the environment"), (azure == 0, "an AZURE_ name is present"), (secret_like == 0, "a secret-like name is present")):
        if not ok:
            emit(f"FAIL environment: {text}")
            problems += 1
    return problems


def reachable(host, port):
    """'connected' for a completed TCP handshake (nothing is sent), 'blocked' for a refusal, a timeout or an unreachable network."""
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return "connected"
    except OSError:
        return "blocked"


def api_answer(host, port):
    """The status an UNAUTHENTICATED request to a namespaced path gets, or None when there is no answer at all."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # no credential is sent, so there is nothing to protect; the answer, not the identity, is asked
    connection = http.client.HTTPSConnection(host, port, timeout=3.0, context=context)
    try:
        connection.request("GET", API_PATH)
        return connection.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def mode_network(args, emit):
    problems = 0
    for label, host, port in FORBIDDEN:
        name = host[1:] if host.startswith("$") else None
        host = os.environ.get(name, "") if name else host
        if not host:
            emit(f"FAIL {label}: the pod has no address for it ({name} is not set)")
            problems += 1
            continue
        problems += judge(emit, f"connect to {label}, port {port}", reachable(host, port), "blocked")
    for label, host, port in (
        ("the API server, service address", os.environ.get("KUBERNETES_SERVICE_HOST", ""), int(os.environ.get("KUBERNETES_SERVICE_PORT", "0") or 0)),
        ("the API server, public address", args.api_public_host, 443),
    ):
        if not host or not port:
            emit(f"FAIL {label}: no address was given")
            problems += 1
            continue
        status = api_answer(host, port)
        refused = status is None or status in DENIED_STATUSES
        emit(f"{'' if refused else 'FAIL '}unauthenticated request to {label}: {'no connection' if status is None else f'http {status}'} (expected no connection, 401 or 403)")
        problems += 0 if refused else 1
    try:
        socket.getaddrinfo("kubernetes.default.svc", 443)
        dns = "resolved"
    except OSError:
        dns = "failed"
    problems += judge(emit, "resolve kubernetes.default.svc", dns, "resolved")
    problems += judge(emit, "connect to the control host, port 443", reachable(args.control_host, 443), "connected")
    return problems


def read_small(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError
        return os.read(fd, MAX_TOKEN_BYTES).decode("ascii").strip()
    finally:
        os.close(fd)


def call(context, host, port, method, path, token, payload=None):
    connection = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=context)
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_BODY_BYTES)
        try:
            document = json.loads(data)
        except ValueError:
            document = None
        return response.status, document
    finally:
        connection.close()


def probe_pod():
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "factory-w-probe-denied", "namespace": WORKER_NS},
        "spec": {"containers": [{"name": "probe", "image": "registry.invalid/probe:latest"}]},
    }


def mode_statuses(args, emit):
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT")
    if not host or not port or not port.isdigit():
        emit("FAIL statuses: this is not a pod with an API server address")
        raise Environment
    try:
        token = read_small(os.path.join(SERVICE_DIR, "token"))
        context = ssl.create_default_context(cafile=os.path.join(SERVICE_DIR, "ca.crt"))
    except (OSError, ssl.SSLError, UnicodeDecodeError):
        emit("FAIL statuses: the service account token or the CA file is not readable")
        raise Environment
    base = f"/api/v1/namespaces/{WORKER_NS}"
    cases = (
        ("not_found", "GET", f"{base}/pods/factory-w-probe-missing", token, None),
        ("forbidden", "GET", f"{base}/secrets", token, None),
        ("unauthorized", "GET", f"{base}/pods", "invalid", None),
        ("denied", "POST", f"{base}/pods?dryRun=All", token, probe_pod()),
    )
    results = []
    for name, method, path, bearer, payload in cases:
        try:
            status, document = call(context, host, int(port), method, path, bearer, payload)
        except (OSError, http.client.HTTPException):
            emit(f"FAIL case {name}: no answer")
            return 1
        results.append({"case": name, "http": status, "body": document})
        if not args.bodies:
            emit(f"case {name}: http {status}")
    if args.bodies:
        emit(json.dumps({"cases": results}, sort_keys=True))
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Probes that run inside a pod of the mirror.")
    parser.add_argument("mode", choices=("socket", "spool", "secret-volume", "environment", "network", "statuses"))
    parser.add_argument("--control-host", default=None)
    parser.add_argument("--api-public-host", default=None)
    parser.add_argument("--expect", choices=("worker", "supervisor", "worker-pod"), default=None)
    parser.add_argument("--as-uid", type=int, default=None)
    parser.add_argument("--as-gid", type=int, default=None)
    parser.add_argument("--bodies", action="store_true")
    args = parser.parse_args(argv)
    if (args.as_uid is None) != (args.as_gid is None):
        parser.error("--as-uid and --as-gid go together")
    if args.mode in ("socket", "spool") and args.expect not in ("worker", "supervisor"):
        parser.error("this mode needs --expect worker or --expect supervisor")
    if args.mode == "network" and not (args.control_host and args.api_public_host):
        parser.error("this mode needs --control-host and --api-public-host")
    if args.mode == "environment" and args.expect not in (None, "worker-pod"):
        parser.error("this mode takes only --expect worker-pod")
    return args


def main(argv=None, emit=print):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    handler = {
        "socket": mode_socket,
        "spool": mode_spool,
        "secret-volume": mode_secret_volume,
        "environment": mode_environment,
        "network": mode_network,
        "statuses": mode_statuses,
    }[args.mode]
    try:
        if args.mode in ("socket", "spool") and args.as_uid is not None:
            try:
                drop_identity(args.as_uid, args.as_gid)
            except OSError:
                emit("FAIL cannot switch to the requested identity (this needs root or CAP_SETUID)")
                return 2
        return 1 if handler(args, emit) else 0
    except Environment:
        return 2


if __name__ == "__main__":
    sys.exit(main())
