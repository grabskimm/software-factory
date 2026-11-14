"""The worker pod contract: every name and number that Terraform and Python must agree on.

This is slice 0 of the pod backend (``docs/pod-backend.md``). Nothing here launches a pod, reads
the environment or talks to Kubernetes; the module only fixes the vocabulary of the two tracks
(``infra/locals.tf`` on one side, the launcher, relay, worker wrapper and supervisor on the other)
before either is written, so that they cannot drift. The backend is off by default and only
``backend.py``, ``podexecution.py`` and ``manager.py`` import this module so far (the default resource class of a launch spec,
the execution switch and the manager instance id).

* ``SHARED`` is the contract tree. The Terraform local ``worker_contract`` in ``infra/locals.tf``
  holds the same tree as literals only, and ``infra/test/test_pod_contract.py`` parses that local
  and fails on any value that differs and on any key that exists on one side only. Every public
  constant that is not listed in ``PYTHON_ONLY`` is read from ``SHARED`` by subscript (a test
  reads this file's syntax tree), so a shared value cannot be retyped here with another number.
* ``PYTHON_ONLY`` names the constants Terraform must not set (timing, byte caps, pacing and
  client bounds). A test proves that no Terraform file mentions them.
* The helpers are pure. They raise ``ValueError`` with a fixed text and never echo the rejected
  value, which may be text from an issue, a run or a job.

Every number marked PROVISIONAL (resource classes, size limits, caps, timings, sidecar uids) is a
first estimate that a read-only production check and a throwaway mirror replace (the footprint
records of ``status --json`` and a measurement on a real deployment); nothing was measured for this
file.

Standard library only and no relative import: ``infra/test/test_pod_contract.py`` loads this file
by path under ``python3 -I`` with no package installed, exactly as CI runs the infra tests.
"""

import hashlib
import re
import secrets
from fractions import Fraction

# --- The tree Terraform repeats ------------------------------------------------------------

SHARED = {
    # Namespaces and object names. The manager namespace and the workload identity subject of
    # infra/locals.tf are existing; the worker namespace and the rest are new and unused until
    # the pod backend is switched on.
    "names": {
        "manager_namespace": "factory",
        "worker_namespace": "factory-workers",
        "worker_service_account": "factory-worker",
        "pod_name_prefix": "factory-w-",
        "secret_name_suffix": "-s",
        "secret_key_token": "token",
        "secret_key_anthropic": "anthropic-api-key",
        "nodepool": "factory-workers",
        "nodeclass": "factory-workers",
        # <registry>/<repository>@sha256:<64 lowercase hex>, the form of the pinned image digest.
        "image_reference_pattern": "^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$",
    },
    # Label keys are <prefix><key>. Values are lowercase hex digests or fixed words, 63 characters
    # at most, alphanumeric at both ends. The repository label is the first 12 hex characters of
    # sha256("<org>/<name>".lower()); the manager label is the manager instance id; readable ids
    # go in annotations, never in labels.
    "labels": {
        "prefix": "factory.internal/",
        "keys": ["role", "repo", "run", "job", "attempt", "phase", "manager", "session"],
        "role_value": "worker",
        "phases": ["plan", "build", "review", "verify", "preflight"],
        "hex_lengths": {"repo": 12, "run": 12, "job": 12, "manager": 12, "session": 32},
    },
    # Annotation keys are <prefix><key>, except the Karpenter one.
    "annotations": {
        "do_not_disrupt": "karpenter.sh/do-not-disrupt",
        "do_not_disrupt_value": "true",
        "keys": ["deadline", "repo", "run-id", "job-id"],
    },
    # The worker node label and taint, and the future manager-pool taint (a later, optional flip).
    "nodes": {
        "worker_label_key": "pool",
        "worker_label_value": "worker",
        "worker_taint_key": "workload",
        "worker_taint_value": "worker",
        "manager_taint_key": "workload",
        "manager_taint_value": "manager",
        "taint_effect": "NoSchedule",
    },
    # The relay port is new. The app and metrics ports repeat the existing locals app_port and
    # metrics_port so that a test sees no clash among the ports of the manager pod.
    "ports": {"relay": 8090, "app": 8080, "metrics": 9464},
    # In-pod paths. /data and /tmp are the worker pod's scratch volumes; the socket directory is
    # the root of an emptyDir shared by the manager and the launcher; the spool is the root of an
    # emptyDir of the relay.
    "paths": {
        "session_secret_dir": "/run/factory-session",
        "scratch_dir": "/data",
        "tmp_dir": "/tmp",
        "launcher_socket_dir": "/run/factory",
        "launcher_socket": "/run/factory/launcher.sock",
        "spool_dir": "/spool",
    },
    # Identities. The supervisor (10001, group 10001) and the worker (10002, group 10010) are the
    # existing image users (Dockerfile). The sidecar uids are PROVISIONAL and must not clash with
    # them; the shared gid is the primary group of both sidecars and the group of the socket
    # directory and of the spool.
    "ids": {
        "supervisor_uid": 10001,
        "worker_uid": 10002,
        "worker_gid": 10010,
        "launcher_uid": 10003,
        "relay_uid": 10004,
        "shared_gid": 10001,
    },
    # Environment variable names. Each name is produced by exactly one Terraform local and read by
    # exactly one Python module (slice 17 adds that check); none appears twice below.
    # FACTORY_MANAGER_INSTANCE is set by the manager for its children, not by Terraform, and is
    # a Python-only constant (MANAGER_INSTANCE_ENV).
    "env": {
        "manager": {
            "pod_backend": "FACTORY_POD_BACKEND",
            "max_pod_sessions": "FACTORY_MAX_POD_SESSIONS",
            "launcher_socket": "FACTORY_POD_LAUNCHER_SOCKET",
            "spool_dir": "FACTORY_POD_SPOOL_DIR",
        },
        # The launcher's immutable configuration: the image, namespace and caps never come from a
        # request.
        "launcher": {
            "image": "FACTORY_LAUNCHER_IMAGE",
            "namespace": "FACTORY_LAUNCHER_NAMESPACE",
            "max_pods": "FACTORY_LAUNCHER_MAX_PODS",
            "repo_cap": "FACTORY_LAUNCHER_REPO_CAP",
            "deadline_clamp": "FACTORY_LAUNCHER_DEADLINE_CLAMP",
            "allowed_uid": "FACTORY_LAUNCHER_ALLOWED_UID",
            "relay_url": "FACTORY_LAUNCHER_RELAY_URL",
            "classes": "FACTORY_LAUNCHER_CLASSES",
            "api_public_host": "FACTORY_LAUNCHER_API_PUBLIC_HOST",
            "manager_address": "FACTORY_LAUNCHER_MANAGER_ADDRESS",
            "manager_metrics_address": "FACTORY_LAUNCHER_MANAGER_METRICS_ADDRESS",
            "socket": "FACTORY_LAUNCHER_SOCKET",
        },
        "relay": {
            "port": "FACTORY_RELAY_PORT",
            "spool_dir": "FACTORY_RELAY_SPOOL_DIR",
        },
        # The worker pod's allow-list: non-secret names only (no name may match KEY, TOKEN,
        # SECRET or PASSWORD, and none may start with AZURE_).
        "pod": [
            "FACTORY_SESSION_ID",
            "FACTORY_RELAY_URL",
            "FACTORY_SESSION_DEADLINE",
            "FACTORY_POD_NAME",
            "FACTORY_POD_UID",
            "FACTORY_NODE_IP",
            "FACTORY_MANAGER_ADDRESS",
            "FACTORY_MANAGER_METRICS_ADDRESS",
            "FACTORY_API_PUBLIC_HOST",
            "AZMON_COLLECT_ENV",
        ],
    },
    "env_values": {"pod_backend_on": "on", "pod_backend_off": "off"},
    # The deadline of a pod is clamp(session timeout) + margin; the admission policy pins the
    # three numbers. PROVISIONAL.
    "timing": {
        "deadline_clamp_seconds": 14400,
        "deadline_margin_seconds": 300,
        "termination_grace_seconds": 30,
    },
    "limits": {"per_repo_cap": 4},
    # Resource classes. PROVISIONAL until the first read-only production check reads the footprint
    # records: requests equal limits, so cpu, memory and ephemeral-storage are the pod's size.
    # pods_per_node is for a D4ds_v5 node (3.86 vCPU allocatable); the two size limits are those of
    # the emptyDir volumes at /data and /tmp. A test keeps at least 2Gi of the ephemeral-storage
    # free for the writable layer and logs.
    "classes": {
        "default": "standard",
        "table": {
            "standard": {
                "cpu": "2",
                "memory": "4Gi",
                "ephemeral_storage": "20Gi",
                "pods_per_node": 1,
                "data_size_limit": "14Gi",
                "tmp_size_limit": "2Gi",
            },
            "small": {
                "cpu": "1",
                "memory": "2Gi",
                "ephemeral_storage": "10Gi",
                "pods_per_node": 3,
                "data_size_limit": "7Gi",
                "tmp_size_limit": "1Gi",
            },
        },
    },
    # Sidecar volumes. Exceeding the spool sizeLimit evicts the manager pod, so the relay and the
    # staging code refuse at SPOOL_REFUSE_BYTES, at most 75% of it (a test on both sides).
    "volumes": {"spool_size_limit": "16Gi", "socket_dir_size_limit": "1Mi"},
    # What the worker's network canary must NOT reach, and the two API-server probes (an
    # unauthenticated request to api_probe_path must answer one of the denied statuses or not
    # connect). host_env and port_env name an environment variable of the pod.
    "canary": {
        "forbidden": [
            {"host": "169.254.169.254", "port": 80},
            {"host": "168.63.129.16", "port": 80},
            {"host": "168.63.129.16", "port": 32526},
            {"host_env": "FACTORY_NODE_IP", "port": 10250},
            {"host_env": "FACTORY_MANAGER_ADDRESS", "port": 8080},
            {"host_env": "FACTORY_MANAGER_METRICS_ADDRESS", "port": 9464},
        ],
        "api_probes": [
            {"name": "service", "host_env": "KUBERNETES_SERVICE_HOST", "port_env": "KUBERNETES_SERVICE_PORT"},
            {"name": "public", "host_env": "FACTORY_API_PUBLIC_HOST", "port": 443},
        ],
        "api_probe_path": "/api/v1/namespaces/factory/secrets",
        "api_probe_denied_statuses": [401, 403],
    },
}

# --- Constants read from the tree ------------------------------------------------------------

MANAGER_NAMESPACE = SHARED["names"]["manager_namespace"]
WORKER_NAMESPACE = SHARED["names"]["worker_namespace"]
WORKER_SERVICE_ACCOUNT = SHARED["names"]["worker_service_account"]
POD_NAME_PREFIX = SHARED["names"]["pod_name_prefix"]
SECRET_NAME_SUFFIX = SHARED["names"]["secret_name_suffix"]
SECRET_KEY_TOKEN = SHARED["names"]["secret_key_token"]
SECRET_KEY_ANTHROPIC = SHARED["names"]["secret_key_anthropic"]
NODEPOOL_NAME = SHARED["names"]["nodepool"]
NODECLASS_NAME = SHARED["names"]["nodeclass"]
IMAGE_REFERENCE_PATTERN = SHARED["names"]["image_reference_pattern"]

LABEL_PREFIX = SHARED["labels"]["prefix"]
LABEL_KEYS = SHARED["labels"]["keys"]
LABEL_ROLE_VALUE = SHARED["labels"]["role_value"]
PHASES = SHARED["labels"]["phases"]
LABEL_HEX_LENGTHS = SHARED["labels"]["hex_lengths"]

ANNOTATION_DO_NOT_DISRUPT = SHARED["annotations"]["do_not_disrupt"]
ANNOTATION_DO_NOT_DISRUPT_VALUE = SHARED["annotations"]["do_not_disrupt_value"]
ANNOTATION_KEYS = SHARED["annotations"]["keys"]

WORKER_NODE_LABEL_KEY = SHARED["nodes"]["worker_label_key"]
WORKER_NODE_LABEL_VALUE = SHARED["nodes"]["worker_label_value"]
WORKER_TAINT_KEY = SHARED["nodes"]["worker_taint_key"]
WORKER_TAINT_VALUE = SHARED["nodes"]["worker_taint_value"]
MANAGER_TAINT_KEY = SHARED["nodes"]["manager_taint_key"]
MANAGER_TAINT_VALUE = SHARED["nodes"]["manager_taint_value"]
TAINT_EFFECT = SHARED["nodes"]["taint_effect"]

RELAY_PORT = SHARED["ports"]["relay"]
APP_PORT = SHARED["ports"]["app"]
METRICS_PORT = SHARED["ports"]["metrics"]

SESSION_SECRET_DIR = SHARED["paths"]["session_secret_dir"]
SCRATCH_DIR = SHARED["paths"]["scratch_dir"]
TMP_DIR = SHARED["paths"]["tmp_dir"]
LAUNCHER_SOCKET_DIR = SHARED["paths"]["launcher_socket_dir"]
LAUNCHER_SOCKET = SHARED["paths"]["launcher_socket"]
SPOOL_DIR = SHARED["paths"]["spool_dir"]

SUPERVISOR_UID = SHARED["ids"]["supervisor_uid"]
WORKER_UID = SHARED["ids"]["worker_uid"]
WORKER_GID = SHARED["ids"]["worker_gid"]
LAUNCHER_UID = SHARED["ids"]["launcher_uid"]
RELAY_UID = SHARED["ids"]["relay_uid"]
SHARED_GID = SHARED["ids"]["shared_gid"]

ENV_MANAGER = SHARED["env"]["manager"]
ENV_LAUNCHER = SHARED["env"]["launcher"]
ENV_RELAY = SHARED["env"]["relay"]
ENV_POD = SHARED["env"]["pod"]
POD_BACKEND_ON = SHARED["env_values"]["pod_backend_on"]
POD_BACKEND_OFF = SHARED["env_values"]["pod_backend_off"]

DEADLINE_CLAMP_SECONDS = SHARED["timing"]["deadline_clamp_seconds"]
DEADLINE_MARGIN_SECONDS = SHARED["timing"]["deadline_margin_seconds"]
TERMINATION_GRACE_SECONDS = SHARED["timing"]["termination_grace_seconds"]

PER_REPO_CAP = SHARED["limits"]["per_repo_cap"]

DEFAULT_CLASS = SHARED["classes"]["default"]
CLASSES = SHARED["classes"]["table"]

SPOOL_SIZE_LIMIT = SHARED["volumes"]["spool_size_limit"]
SOCKET_DIR_SIZE_LIMIT = SHARED["volumes"]["socket_dir_size_limit"]

CANARY_FORBIDDEN = SHARED["canary"]["forbidden"]
CANARY_API_PROBES = SHARED["canary"]["api_probes"]
CANARY_API_PROBE_PATH = SHARED["canary"]["api_probe_path"]
CANARY_API_DENIED_STATUSES = SHARED["canary"]["api_probe_denied_statuses"]

# --- Python-only constants (Terraform must not set them) ------------------------------------
# All PROVISIONAL. Real-repository throughput was never measured; a later slice measures it.

_KIB = 1024
_MIB = 1024 * _KIB
_GIB = 1024 * _MIB

# Set by the manager for its children (a fresh random id at every manager process start).
MANAGER_INSTANCE_ENV = "FACTORY_MANAGER_INSTANCE"

# Timing, seconds.
HEARTBEAT_SECONDS = 30
ORPHAN_SILENCE_SECONDS = 300
SCHEDULING_TIMEOUT_SECONDS = 600
PULL_ERROR_SECONDS = 120
LAUNCH_GRACE_SECONDS = 120
ORPHAN_SWEEP_SECONDS = 120
SPOOL_RETENTION_SECONDS = 3600

# Relay caps.
BUNDLE_MAX_BYTES = 1 * _GIB
DELTA_MAX_BYTES = 256 * _MIB
DELTA_MAX_FILES = 20000
FILE_MAX_BYTES = 64 * _MIB
PATH_MAX_BYTES = 4096
TRANSCRIPT_MAX_BYTES = 512 * _MIB
STDERR_MAX_BYTES = 16 * _MIB
RELAY_IDLE_TIMEOUT_SECONDS = 60
RELAY_STREAM_TOTAL_SECONDS = 120
RELAY_TRANSFER_TOTAL_SECONDS = 900
RELAY_MAX_THREADS = 32
# The relay and the staging code refuse new spool data above this; at most 75% of SPOOL_SIZE_LIMIT.
SPOOL_REFUSE_BYTES = 12 * _GIB

# Pacing of launches: a burst, then one session per interval.
PACING_BURST = 2
PACING_INTERVAL_SECONDS = 15

# Kubernetes client bounds.
KUBE_CONNECT_TIMEOUT_SECONDS = 5
KUBE_READ_TIMEOUT_SECONDS = 30
KUBE_RETRIES = 3
KUBE_RETRY_BACKOFF_SECONDS = (1, 2, 4)

PYTHON_ONLY = (
    "MANAGER_INSTANCE_ENV",
    "HEARTBEAT_SECONDS",
    "ORPHAN_SILENCE_SECONDS",
    "SCHEDULING_TIMEOUT_SECONDS",
    "PULL_ERROR_SECONDS",
    "LAUNCH_GRACE_SECONDS",
    "ORPHAN_SWEEP_SECONDS",
    "SPOOL_RETENTION_SECONDS",
    "BUNDLE_MAX_BYTES",
    "DELTA_MAX_BYTES",
    "DELTA_MAX_FILES",
    "FILE_MAX_BYTES",
    "PATH_MAX_BYTES",
    "TRANSCRIPT_MAX_BYTES",
    "STDERR_MAX_BYTES",
    "RELAY_IDLE_TIMEOUT_SECONDS",
    "RELAY_STREAM_TOTAL_SECONDS",
    "RELAY_TRANSFER_TOTAL_SECONDS",
    "RELAY_MAX_THREADS",
    "SPOOL_REFUSE_BYTES",
    "PACING_BURST",
    "PACING_INTERVAL_SECONDS",
    "KUBE_CONNECT_TIMEOUT_SECONDS",
    "KUBE_READ_TIMEOUT_SECONDS",
    "KUBE_RETRIES",
    "KUBE_RETRY_BACKOFF_SECONDS",
)

# --- Syntax ----------------------------------------------------------------------------------

# GitHub's own limits for an owner and a repository name; the same shapes the rest of the package
# accepts. Run and job ids are the shapes of state.py and the manager API.
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# Kubernetes label values, restricted to lowercase: at most 63 characters, alphanumeric at both ends.
_LABEL_VALUE = re.compile(r"[a-z0-9]([a-z0-9_.-]{0,61}[a-z0-9])?")
_ATTEMPT = re.compile(r"[0-9]{1,4}")
_QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?")
_QUANTITY_FACTORS = {
    "": Fraction(1),
    "m": Fraction(1, 1000),
    "k": Fraction(1000),
    "M": Fraction(1000**2),
    "G": Fraction(1000**3),
    "T": Fraction(1000**4),
    "P": Fraction(1000**5),
    "E": Fraction(1000**6),
    "Ki": Fraction(1024),
    "Mi": Fraction(1024**2),
    "Gi": Fraction(1024**3),
    "Ti": Fraction(1024**4),
    "Pi": Fraction(1024**5),
    "Ei": Fraction(1024**6),
}


def _hex_pattern(length):
    return re.compile(f"[0-9a-f]{{{length}}}")


_SESSION_ID = _hex_pattern(LABEL_HEX_LENGTHS["session"])
_INSTANCE_ID = _hex_pattern(LABEL_HEX_LENGTHS["manager"])
_POD_NAME = re.compile(f"{re.escape(POD_NAME_PREFIX)}([0-9a-f]{{{LABEL_HEX_LENGTHS['session']}}})")


def _text(value, pattern, what):
    """`value` when it is a string that matches `pattern` completely; a fixed ValueError otherwise."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Helpers ----------------------------------------------------------------------------------


def validate_label_value(value):
    """`value` when it is a valid label value of this contract, else ValueError (the text is not echoed)."""
    return _text(value, _LABEL_VALUE, "label value")


def validate_session_id(value):
    """A session id: lowercase hex, as long as the contract says (it is also a label value and a pod name suffix)."""
    return _text(value, _SESSION_ID, "session id")


def validate_instance_id(value):
    """A manager instance id: lowercase hex, as long as the contract says."""
    return _text(value, _INSTANCE_ID, "manager instance id")


def new_session_id():
    """A fresh random session id."""
    return secrets.token_hex(LABEL_HEX_LENGTHS["session"] // 2)


def new_instance_id():
    """A fresh random manager instance id: drawn at EVERY manager process start and kept in memory.

    A pod that carries an older id was launched by a previous manager process.
    """
    return secrets.token_hex(LABEL_HEX_LENGTHS["manager"] // 2)


def repo_key(repository):
    """The repository label value: the first 12 hex characters of sha256("<org>/<name>".lower())."""
    _text(repository, _REPOSITORY, "repository")
    return _digest(repository.lower())[: LABEL_HEX_LENGTHS["repo"]]


def pod_name(session_id):
    """The pod name of a session: the contract prefix and the session id."""
    return f"{POD_NAME_PREFIX}{validate_session_id(session_id)}"


def secret_name(pod):
    """The per-session Secret name: the pod name and the contract suffix (`<pod name>-s`)."""
    _text(pod, _POD_NAME, "pod name")
    return f"{pod}{SECRET_NAME_SUFFIX}"


def label_key(name):
    """The full label key of one of LABEL_KEYS."""
    if name not in LABEL_KEYS:
        raise ValueError("unknown label key")
    return f"{LABEL_PREFIX}{name}"


def annotation_key(name):
    """The full annotation key of one of ANNOTATION_KEYS."""
    if name not in ANNOTATION_KEYS:
        raise ValueError("unknown annotation key")
    return f"{LABEL_PREFIX}{name}"


def worker_labels(repository, run_id, job_id, attempt, phase, manager_instance, session_id):
    """The labels of a worker pod, keyed by the full label keys.

    Every value is a lowercase hex digest or a fixed word: the run and job ids are hashed the way
    the repository is (their readable form goes in the annotations).
    """
    _text(run_id, _RUN_ID, "run id")
    _text(job_id, _JOB_ID, "job id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not _ATTEMPT.fullmatch(str(attempt)):
        raise ValueError("invalid attempt")
    if phase not in PHASES:
        raise ValueError("invalid phase")
    values = {
        "role": LABEL_ROLE_VALUE,
        "repo": repo_key(repository),
        "run": _digest(run_id)[: LABEL_HEX_LENGTHS["run"]],
        "job": _digest(job_id)[: LABEL_HEX_LENGTHS["job"]],
        "attempt": str(attempt),
        "phase": phase,
        "manager": validate_instance_id(manager_instance),
        "session": validate_session_id(session_id),
    }
    return {label_key(key): validate_label_value(values[key]) for key in LABEL_KEYS}


def worker_annotations(repository, run_id, job_id, deadline_epoch):
    """The annotations of a worker pod: do-not-disrupt and the readable ids (never issue text or titles)."""
    _text(repository, _REPOSITORY, "repository")
    _text(run_id, _RUN_ID, "run id")
    _text(job_id, _JOB_ID, "job id")
    if isinstance(deadline_epoch, bool) or not isinstance(deadline_epoch, int) or deadline_epoch < 0:
        raise ValueError("invalid deadline")
    return {
        ANNOTATION_DO_NOT_DISRUPT: ANNOTATION_DO_NOT_DISRUPT_VALUE,
        annotation_key("deadline"): str(deadline_epoch),
        annotation_key("repo"): repository,
        annotation_key("run-id"): run_id,
        annotation_key("job-id"): job_id,
    }


def active_deadline_seconds(timeout_seconds):
    """`activeDeadlineSeconds` of a pod: the session timeout clamped to the contract, plus the margin."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("invalid timeout")
    return min(timeout_seconds, DEADLINE_CLAMP_SECONDS) + DEADLINE_MARGIN_SECONDS


def parse_quantity(text):
    """A Kubernetes quantity as an exact Fraction (cpu in cores, memory and storage in bytes).

    Plain numbers with the suffixes m, k, M, G, T, P, E, Ki, Mi, Gi, Ti, Pi and Ei; exponent
    notation and signs are not accepted.
    """
    found = _QUANTITY.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        raise ValueError("invalid quantity")
    return Fraction(found.group(1)) * _QUANTITY_FACTORS[found.group(2) or ""]
