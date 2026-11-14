"""The worker pod template and a Python mirror of its admission policy.

This is slice 6 of the pod backend (``docs/pod-backend.md``). Nothing here launches a pod, reads
the environment or talks to Kubernetes, and nothing in the package imports this module yet. It is
the single source of the pod design:

* ``build_pod(request, config)`` turns a CLOSED request into the manifest of one worker pod, and
  ``build_secret(pod, token, key)`` into the manifest of its per-session Secret. Both are plain
  dicts, built from the contract of ``podcontract`` (names, labels, ports, paths, classes, caps)
  and from nothing else: the image, the namespace and the caps never come from the request.
* ``check_policy`` and ``check_secret_policy`` are a Python MIRROR of the intended admission
  policies. Every rule has a stable id (``POLICY_RULES``) and the mirror answers with the ids it
  breaks. The mirror is an offline model: it is NOT evidence that the CEL of the real
  ValidatingAdmissionPolicy (a later slice) works. It judges the object as the launcher builds
  it; the API server defaults and mutates a pod before validating admission (for example it adds
  two NoExecute tolerations and a scheduler name), which this model does not reproduce.
* ``apply_patch`` is a tiny RFC 6902 subset (add, remove, replace) so that the golden mutation
  files can be replayed by a test here and by a tool against a real API server later.

The mirror fails closed: a missing field, a field of the wrong type or an unknown key is a
violation, never an exception. So is a value that a rule cannot walk (nested so deeply that reading
it raises a RecursionError, or any other failure while reading): that rule is broken, and no
exception reaches the caller. Errors raised by the builders carry fixed texts and never echo the
rejected value, which may come from an issue, a run or a job. No secret value is a constant here:
the Secret values are arguments and appear only in the base64 of the Secret they are written to.

Every number marked PROVISIONAL in ``podcontract`` (classes, size limits, the deadline numbers)
is read from there; the wrapper command is a fixed path that only a real image can confirm.
"""

import base64
import copy
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import podcontract as pc

# --- Fixed parts of the template ------------------------------------------------------------

CONTAINER_NAME = "worker"
# The root wrapper (slice 10a, `python -m factory.podworker`) replaces the image entrypoint, so it
# is run under tini like the manager. The paths are those of image/README.md (UNVERIFIED in a pod).
WORKER_COMMAND = ("/usr/bin/tini", "--", "/opt/factory/bin/python", "-m", "factory.podworker")
IMAGE_PULL_POLICY = "IfNotPresent"

VOLUME_DATA = "data"
VOLUME_TMP = "tmp"
VOLUME_SESSION = "session"
SESSION_SECRET_MODE = 0o400  # root-owned 0400: only the root wrapper reads the token and the key

NODE_OS_LABEL = "kubernetes.io/os"
NODE_OS_VALUE = "linux"
HOSTNAME_KEY = "kubernetes.io/hostname"
IDENTITY_LABEL = "azure.workload.identity/use"

COLLECT_ENV = "AZMON_COLLECT_ENV"
COLLECT_ENV_VALUE = "False"
# The only names that come from the downward API, and the field each one reads.
FIELD_REFS = {
    "FACTORY_POD_NAME": "metadata.name",
    "FACTORY_POD_UID": "metadata.uid",
    "FACTORY_NODE_IP": "status.hostIP",
}
PLAIN_ENV = (
    "FACTORY_SESSION_ID",
    "FACTORY_RELAY_URL",
    "FACTORY_SESSION_DEADLINE",
    "FACTORY_MANAGER_ADDRESS",
    "FACTORY_MANAGER_METRICS_ADDRESS",
    "FACTORY_API_PUBLIC_HOST",
    COLLECT_ENV,
)

REQUEST_FIELDS = (
    "session_id",
    "repository",
    "run_id",
    "job_id",
    "attempt",
    "phase",
    "resource_class",
    "timeout_seconds",
    "manager_instance",
)

_RELAY_URL = re.compile(f"http://[A-Za-z0-9.-]{{1,253}}:{pc.RELAY_PORT}")
_ADDRESS = re.compile(r"[A-Za-z0-9.:-]{1,253}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SECRET_VALUE = re.compile(r"[\x21-\x7e]{1,4096}")
_DIGITS = re.compile(r"[0-9]{1,12}")

# --- The policy rules (stable ids; the CEL of the later slice carries the same ones) ----------

POD_RULES = (
    "image",
    "host-users",
    "host-network",
    "host-pid",
    "host-ipc",
    "node-name",
    "host-path",
    "privileged",
    "capabilities",
    "run-as",
    "seccomp",
    "proc-mount",
    "security-context-keys",
    "service-account",
    "automount",
    "volumes",
    "volume-mounts",
    "env-names",
    "env-source",
    "env-collect",
    "node-selector",
    "tolerations",
    "anti-affinity",
    "restart-policy",
    "deadline",
    "grace",
    "resources",
    "labels",
    "annotations",
    "identity-label",
    "owner-references",
    "containers",
    "container-fields",
    "command",
    "pod-name",
    "pod-metadata",
    "spec-fields",
    "service-links",
)
SECRET_RULES = (
    "secret-namespace",
    "secret-name",
    "secret-type",
    "secret-keys",
    "secret-owner",
    "secret-fields",
)
POLICY_RULES = (*POD_RULES, *SECRET_RULES)

# The pod spec keys the template sets; every other key is refused (spec-fields) unless a rule of
# its own names it.
_SPEC_KEYS = frozenset(
    {
        "restartPolicy",
        "activeDeadlineSeconds",
        "terminationGracePeriodSeconds",
        "hostUsers",
        "serviceAccountName",
        "automountServiceAccountToken",
        "enableServiceLinks",
        "nodeSelector",
        "tolerations",
        "affinity",
        "containers",
        "volumes",
    }
)
# Spec keys that have a rule of their own, so spec-fields does not repeat them.
_SPEC_KEYS_WITH_OWN_RULE = frozenset(
    {
        "hostNetwork",
        "hostPID",
        "hostIPC",
        "nodeName",
        "initContainers",
        "ephemeralContainers",
        "securityContext",
        "serviceAccount",
    }
)
_CONTAINER_KEYS = frozenset(
    {"name", "image", "imagePullPolicy", "command", "env", "resources", "securityContext", "volumeMounts"}
)
_RESOURCE_NAMES = ("cpu", "memory", "ephemeral-storage")
_RESOURCE_CLASS_KEYS = {"cpu": "cpu", "memory": "memory", "ephemeral-storage": "ephemeral_storage"}
_SECURITY_CONTEXT = {"procMount": "Unmasked", "appArmorProfile": {"type": "Unconfined"}}
_SECRET_TOP_KEYS = frozenset({"apiVersion", "kind", "metadata", "type", "data"})
_SECRET_META_KEYS = frozenset({"name", "namespace", "ownerReferences"})
_MISSING = object()


# --- Builders -------------------------------------------------------------------------------


def _text(value, pattern, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


@dataclass(frozen=True)
class PodConfig:
    """What the launcher itself knows (its immutable environment); none of it comes from a request.

    `image` is the pinned digest reference, `relay_url` the relay address of the manager pod,
    `manager_address`, `manager_metrics_address` (the Service that exposes the metrics port) and `api_public_host` the plain values the network canary needs. `clock`
    returns the current epoch seconds (a test passes a fixed one).
    """

    image: str
    relay_url: str
    manager_address: str
    manager_metrics_address: str
    api_public_host: str
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        _text(self.image, re.compile(pc.IMAGE_REFERENCE_PATTERN), "image reference")
        _text(self.relay_url, _RELAY_URL, "relay url")
        _text(self.manager_address, _ADDRESS, "manager address")
        _text(self.manager_metrics_address, _ADDRESS, "manager metrics address")
        _text(self.api_public_host, _HOST, "api host")
        if not callable(self.clock):
            raise ValueError("invalid clock")


def validate_request(request):
    """A copy of `request` when it is exactly the closed request of a launch, else ValueError.

    Unknown fields, missing fields and a class outside the contract enum are errors. The other
    values are validated by the contract helpers while the pod is built.
    """
    if not isinstance(request, Mapping):
        raise ValueError("invalid request")
    keys = set(request)
    if keys - set(REQUEST_FIELDS):
        raise ValueError("unknown request field")
    if set(REQUEST_FIELDS) - keys:
        raise ValueError("missing request field")
    resource_class = request["resource_class"]
    if not isinstance(resource_class, str) or resource_class not in pc.CLASSES:
        raise ValueError("invalid resource class")
    return {name: request[name] for name in REQUEST_FIELDS}


def _now(config):
    now = config.clock()
    if isinstance(now, bool) or not isinstance(now, (int, float)) or now < 0:
        raise ValueError("invalid clock")
    return int(now)


def _environment(request, config, deadline_epoch):
    plain = {
        "FACTORY_SESSION_ID": request["session_id"],
        "FACTORY_RELAY_URL": config.relay_url,
        "FACTORY_SESSION_DEADLINE": str(deadline_epoch),
        "FACTORY_MANAGER_ADDRESS": config.manager_address,
        "FACTORY_MANAGER_METRICS_ADDRESS": config.manager_metrics_address,
        "FACTORY_API_PUBLIC_HOST": config.api_public_host,
        COLLECT_ENV: COLLECT_ENV_VALUE,
    }
    entries = []
    for name in pc.ENV_POD:
        if name in FIELD_REFS:
            entries.append({"name": name, "valueFrom": {"fieldRef": {"fieldPath": FIELD_REFS[name]}}})
        elif name in PLAIN_ENV:
            entries.append({"name": name, "value": plain[name]})
        else:
            raise ValueError("unmapped environment name")
    return entries


def _sizes(resource_class):
    row = pc.CLASSES[resource_class]
    return {name: row[key] for name, key in _RESOURCE_CLASS_KEYS.items()}


def _volumes(resource_class, pod):
    row = pc.CLASSES[resource_class]
    return [
        {"name": VOLUME_DATA, "emptyDir": {"sizeLimit": row["data_size_limit"]}},
        {"name": VOLUME_TMP, "emptyDir": {"sizeLimit": row["tmp_size_limit"]}},
        {"name": VOLUME_SESSION, "secret": {"secretName": pc.secret_name(pod), "defaultMode": SESSION_SECRET_MODE}},
    ]


def _mounts():
    return [
        {"name": VOLUME_DATA, "mountPath": pc.SCRATCH_DIR},
        {"name": VOLUME_TMP, "mountPath": pc.TMP_DIR},
        {"name": VOLUME_SESSION, "mountPath": pc.SESSION_SECRET_DIR, "readOnly": True},
    ]


def _anti_affinity(repo_label):
    """Required: no node that holds a pod of this namespace whose repo label is not this repository's."""
    term = {
        "labelSelector": {
            "matchExpressions": [{"key": pc.label_key("repo"), "operator": "NotIn", "values": [repo_label]}]
        },
        "topologyKey": HOSTNAME_KEY,
    }
    return {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [term]}}


def _toleration():
    return {
        "key": pc.WORKER_TAINT_KEY,
        "operator": "Equal",
        "value": pc.WORKER_TAINT_VALUE,
        "effect": pc.TAINT_EFFECT,
    }


def build_pod(request, config):
    """The manifest of one worker pod, as a fresh dict, from a closed request and the launcher's config."""
    fields = validate_request(request)
    if not isinstance(config, PodConfig):
        raise ValueError("invalid config")
    session_id = fields["session_id"]
    name = pc.pod_name(session_id)
    seconds = pc.active_deadline_seconds(fields["timeout_seconds"])
    deadline = _now(config) + seconds
    labels = pc.worker_labels(
        fields["repository"],
        fields["run_id"],
        fields["job_id"],
        fields["attempt"],
        fields["phase"],
        fields["manager_instance"],
        session_id,
    )
    annotations = pc.worker_annotations(fields["repository"], fields["run_id"], fields["job_id"], deadline)
    sizes = _sizes(fields["resource_class"])
    container = {
        "name": CONTAINER_NAME,
        "image": config.image,
        "imagePullPolicy": IMAGE_PULL_POLICY,
        "command": list(WORKER_COMMAND),
        "env": _environment(fields, config, deadline),
        "resources": {"requests": dict(sizes), "limits": dict(sizes)},
        "securityContext": copy.deepcopy(_SECURITY_CONTEXT),
        "volumeMounts": _mounts(),
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": pc.WORKER_NAMESPACE, "labels": labels, "annotations": annotations},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": seconds,
            "terminationGracePeriodSeconds": pc.TERMINATION_GRACE_SECONDS,
            "hostUsers": False,
            "serviceAccountName": pc.WORKER_SERVICE_ACCOUNT,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "nodeSelector": {pc.WORKER_NODE_LABEL_KEY: pc.WORKER_NODE_LABEL_VALUE, NODE_OS_LABEL: NODE_OS_VALUE},
            "tolerations": [_toleration()],
            "affinity": _anti_affinity(labels[pc.label_key("repo")]),
            "containers": [container],
            "volumes": _volumes(fields["resource_class"], name),
        },
    }


def _secret_value(value):
    if not isinstance(value, str) or not _SECRET_VALUE.fullmatch(value):
        raise ValueError("invalid secret value")
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def _created_pod(pod):
    """The name and uid of a pod as the API returns it after creation, else ValueError."""
    name = _at(pod, "metadata", "name")
    uid = _at(pod, "metadata", "uid")
    namespace = _at(pod, "metadata", "namespace")
    if namespace is not _MISSING and namespace != pc.WORKER_NAMESPACE:
        raise ValueError("invalid pod")
    pc.secret_name(name)  # raises ValueError for anything but a worker pod name
    _text(uid, _UID, "pod uid")
    return name, uid


def build_secret(pod, token, key):
    """The Opaque Secret of a pod: the relay token and the Anthropic key, owned by that pod.

    `pod` is the pod as the API returned it (a name and a uid). The owner reference has no
    `blockOwnerDeletion`, so no finalizer permission is needed. The values are arguments only;
    they are never constants, never in an error text and never in a golden file.
    """
    name, uid = _created_pod(pod)
    values = {pc.SECRET_KEY_TOKEN: _secret_value(token), pc.SECRET_KEY_ANTHROPIC: _secret_value(key)}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": pc.secret_name(name),
            "namespace": pc.WORKER_NAMESPACE,
            "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": name, "uid": uid}],
        },
        "type": "Opaque",
        "data": values,
    }


# --- Helpers of the mirror (a malformed object is a False; a failure to read one is caught by _complies) ---


def _child(value, step):
    if isinstance(step, int):
        found = isinstance(value, list) and 0 <= step < len(value)
        return value[step] if found else _MISSING
    return value.get(step, _MISSING) if isinstance(value, dict) else _MISSING


def _at(root, *path):
    """The value at `path` of nested dicts and lists, or _MISSING."""
    current = root
    for step in path:
        current = _child(current, step)
        if current is _MISSING:
            break
    return current


def _same(left, right):
    """Deep equality that tells a boolean from a number (True is not 1)."""
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return False


def _is_int(value):
    return type(value) is int  # type(), not isinstance: a bool is not a number


def _quantity(value):
    try:
        return pc.parse_quantity(value)
    except ValueError:
        return None


def _spec(pod):
    spec = _at(pod, "spec")
    return spec if isinstance(spec, dict) else {}


def _labels(pod):
    labels = _at(pod, "metadata", "labels")
    return labels if isinstance(labels, dict) else {}


def _all_containers(pod):
    spec = _spec(pod)
    found = []
    for key in ("containers", "initContainers", "ephemeralContainers"):
        entries = spec.get(key)
        if isinstance(entries, list):
            found.extend(entry for entry in entries if isinstance(entry, dict))
    return found


def _context(container):
    context = container.get("securityContext")
    return context if isinstance(context, dict) else {}


def _contexts(pod):
    """Every securityContext of the pod: one per container and the pod-level one."""
    found = [_context(container) for container in _all_containers(pod)]
    pod_level = _spec(pod).get("securityContext")
    if isinstance(pod_level, dict):
        found.append(pod_level)
    return found


def _absent_or_false(value):
    return value is _MISSING or value is False


def _valid_pod_name(name):
    try:
        pc.secret_name(name)
    except ValueError:
        return False
    return True


def _maxima(key_of):
    """The largest value any resource class allows, per name (the policy pins the maximum, not a class)."""
    return {name: max(pc.parse_quantity(row[key_of[name]]) for row in pc.CLASSES.values()) for name in key_of}


# --- Pod rules: each returns True when the pod complies --------------------------------------------


def _image_ok(pod, allowed_image):
    if not isinstance(allowed_image, str) or not re.fullmatch(pc.IMAGE_REFERENCE_PATTERN, allowed_image):
        return False
    return all(container.get("image") == allowed_image for container in _all_containers(pod))


def _host_users_ok(pod):
    return _spec(pod).get("hostUsers", _MISSING) is False


def _host_network_ok(pod):
    return _absent_or_false(_spec(pod).get("hostNetwork", _MISSING))


def _host_pid_ok(pod):
    return _absent_or_false(_spec(pod).get("hostPID", _MISSING))


def _host_ipc_ok(pod):
    return _absent_or_false(_spec(pod).get("hostIPC", _MISSING))


def _node_name_ok(pod):
    return "nodeName" not in _spec(pod)


def _host_path_ok(pod):
    volumes = _spec(pod).get("volumes")
    if not isinstance(volumes, list):
        return True
    return not any(isinstance(volume, dict) and "hostPath" in volume for volume in volumes)


def _privileged_ok(pod):
    return all(_absent_or_false(context.get("privileged", _MISSING)) for context in _contexts(pod))


def _capabilities_ok(pod):
    return all("capabilities" not in context for context in _contexts(pod))


def _run_as_ok(pod):
    return all(not {"runAsUser", "runAsGroup", "runAsNonRoot"} & set(context) for context in _contexts(pod))


def _seccomp_ok(pod):
    return all("seccompProfile" not in context for context in _contexts(pod))


def _proc_mount_ok(pod):
    return all(_context(container).get("procMount") == "Unmasked" for container in _all_containers(pod))


def _security_context_keys_ok(pod):
    if "securityContext" in _spec(pod):
        return False
    return all(_same(container.get("securityContext"), _SECURITY_CONTEXT) for container in _all_containers(pod))


def _service_account_ok(pod):
    spec = _spec(pod)
    if spec.get("serviceAccountName") != pc.WORKER_SERVICE_ACCOUNT:
        return False
    return spec.get("serviceAccount", pc.WORKER_SERVICE_ACCOUNT) == pc.WORKER_SERVICE_ACCOUNT


def _automount_ok(pod):
    return _spec(pod).get("automountServiceAccountToken", _MISSING) is False


def _scratch_ok(volume, limit_key):
    if not _same(sorted(volume), ["emptyDir", "name"]):
        return False
    scratch = volume["emptyDir"]
    if not isinstance(scratch, dict) or set(scratch) != {"sizeLimit"}:
        return False
    limit = _quantity(scratch["sizeLimit"])
    return limit is not None and 0 < limit <= max(pc.parse_quantity(row[limit_key]) for row in pc.CLASSES.values())


def _session_volume_ok(volume, pod):
    name = _at(pod, "metadata", "name")
    if not _valid_pod_name(name) or sorted(volume) != ["name", "secret"]:
        return False
    expected = {"secretName": pc.secret_name(name), "defaultMode": SESSION_SECRET_MODE}
    return _same(volume["secret"], expected)


def _volumes_ok(pod):
    volumes = _spec(pod).get("volumes")
    if not isinstance(volumes, list) or not all(isinstance(volume, dict) for volume in volumes):
        return False
    by_name = {volume.get("name"): volume for volume in volumes}
    if len(by_name) != len(volumes) or set(by_name) != {VOLUME_DATA, VOLUME_TMP, VOLUME_SESSION}:
        return False
    return (
        _scratch_ok(by_name[VOLUME_DATA], "data_size_limit")
        and _scratch_ok(by_name[VOLUME_TMP], "tmp_size_limit")
        and _session_volume_ok(by_name[VOLUME_SESSION], pod)
    )


def _volume_mounts_ok(pod):
    expected = sorted(_mounts(), key=lambda mount: mount["name"])
    for container in _all_containers(pod):
        mounts = container.get("volumeMounts")
        if not isinstance(mounts, list) or not all(isinstance(mount, dict) for mount in mounts):
            return False
        if not _same(sorted(mounts, key=lambda mount: str(mount.get("name"))), expected):
            return False
    return True


def _env_list(container):
    env = container.get("env", [])
    return env if isinstance(env, list) else None


def _env_names_ok(pod):
    for container in _all_containers(pod):
        env = _env_list(container)
        names = [entry.get("name") for entry in env or [] if isinstance(entry, dict)]
        # The allow-list is the guard: a name outside it (a secret-like one, an AZURE_ one) is refused
        # here, and so is a name that is missing or repeated.
        if env is None or len(names) != len(env) or sorted(map(str, names)) != sorted(pc.ENV_POD):
            return False
    return True


def _env_entry_ok(entry, labels, annotations):
    name = entry.get("name")
    if name in FIELD_REFS:
        return _same(entry, {"name": name, "valueFrom": {"fieldRef": {"fieldPath": FIELD_REFS[name]}}})
    value = entry.get("value")
    if sorted(entry) != ["name", "value"] or not isinstance(value, str):
        return False
    if name == "FACTORY_SESSION_ID":
        return value == labels.get(pc.label_key("session"))
    if name == "FACTORY_SESSION_DEADLINE":
        return value == annotations.get(pc.annotation_key("deadline"))
    patterns = {
        "FACTORY_RELAY_URL": _RELAY_URL,
        "FACTORY_MANAGER_ADDRESS": _ADDRESS,
        "FACTORY_MANAGER_METRICS_ADDRESS": _ADDRESS,
        "FACTORY_API_PUBLIC_HOST": _HOST,
    }
    if name in patterns:
        return patterns[name].fullmatch(value) is not None
    return name == COLLECT_ENV


def _env_source_ok(pod):
    labels = _labels(pod)
    annotations = _at(pod, "metadata", "annotations")
    annotations = annotations if isinstance(annotations, dict) else {}
    for container in _all_containers(pod):
        env = _env_list(container)
        if env is None or "envFrom" in container:
            return False
        if not all(isinstance(entry, dict) and _env_entry_ok(entry, labels, annotations) for entry in env):
            return False
    return True


def _env_collect_ok(pod):
    wanted = {"name": COLLECT_ENV, "value": COLLECT_ENV_VALUE}
    for container in _all_containers(pod):
        env = _env_list(container) or []
        if sum(1 for entry in env if _same(entry, wanted)) != 1:
            return False
    return True


def _node_selector_ok(pod):
    wanted = {pc.WORKER_NODE_LABEL_KEY: pc.WORKER_NODE_LABEL_VALUE, NODE_OS_LABEL: NODE_OS_VALUE}
    return _same(_spec(pod).get("nodeSelector"), wanted)


def _tolerations_ok(pod):
    return _same(_spec(pod).get("tolerations"), [_toleration()])


def _anti_affinity_ok(pod):
    repo = _labels(pod).get(pc.label_key("repo"))
    if not isinstance(repo, str) or not repo:
        return False
    return _same(_spec(pod).get("affinity"), _anti_affinity(repo))


def _restart_policy_ok(pod):
    return _spec(pod).get("restartPolicy") == "Never"


def _deadline_ok(pod):
    seconds = _spec(pod).get("activeDeadlineSeconds")
    return _is_int(seconds) and 1 <= seconds <= pc.DEADLINE_CLAMP_SECONDS + pc.DEADLINE_MARGIN_SECONDS


def _grace_ok(pod):
    seconds = _spec(pod).get("terminationGracePeriodSeconds")
    return _is_int(seconds) and 0 <= seconds <= pc.TERMINATION_GRACE_SECONDS


def _sized(block):
    """name -> Fraction for a requests or limits block of exactly the three resources, else None."""
    if not isinstance(block, dict) or set(block) != set(_RESOURCE_NAMES):
        return None
    sizes = {name: _quantity(block[name]) for name in _RESOURCE_NAMES}
    return None if any(size is None for size in sizes.values()) else sizes


def _resources_ok(pod):
    maxima = _maxima(_RESOURCE_CLASS_KEYS)
    for container in _all_containers(pod):
        resources = container.get("resources")
        if not isinstance(resources, dict) or set(resources) != {"requests", "limits"}:
            return False
        requests = _sized(resources["requests"])
        limits = _sized(resources["limits"])
        if requests is None or requests != limits:
            return False
        if not all(0 < requests[name] <= maxima[name] for name in _RESOURCE_NAMES):
            return False
    return True


def _label_values_ok(labels, name):
    lengths = pc.LABEL_HEX_LENGTHS
    hexes = {key: re.compile(f"[0-9a-f]{{{lengths[key]}}}") for key in ("repo", "run", "job", "manager", "session")}
    values = {key: labels[pc.label_key(key)] for key in pc.LABEL_KEYS}
    if not all(isinstance(value, str) for value in values.values()):
        return False
    if any(hexes[key].fullmatch(values[key]) is None for key in hexes):
        return False
    if values["role"] != pc.LABEL_ROLE_VALUE or values["phase"] not in pc.PHASES:
        return False
    if re.fullmatch(r"[0-9]{1,4}", values["attempt"]) is None:
        return False
    return name == pc.pod_name(values["session"])


def _labels_ok(pod):
    labels = _at(pod, "metadata", "labels")
    name = _at(pod, "metadata", "name")
    if not isinstance(labels, dict) or not _valid_pod_name(name):
        return False
    if set(labels) != {pc.label_key(key) for key in pc.LABEL_KEYS}:
        return False
    return _label_values_ok(labels, name)


def _annotations_ok(pod):
    annotations = _at(pod, "metadata", "annotations")
    if not isinstance(annotations, dict):
        return False
    deadline = annotations.get(pc.annotation_key("deadline"))
    if not isinstance(deadline, str) or _DIGITS.fullmatch(deadline) is None:
        return False
    try:
        wanted = pc.worker_annotations(
            annotations.get(pc.annotation_key("repo")),
            annotations.get(pc.annotation_key("run-id")),
            annotations.get(pc.annotation_key("job-id")),
            int(deadline),
        )
    except ValueError:
        return False
    return _same(annotations, wanted)


def _identity_label_ok(pod):
    return IDENTITY_LABEL not in _labels(pod)


def _owner_references_ok(pod):
    metadata = _at(pod, "metadata")
    return not (isinstance(metadata, dict) and "ownerReferences" in metadata)


def _containers_ok(pod):
    spec = _spec(pod)
    if "initContainers" in spec or "ephemeralContainers" in spec:
        return False
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        return False
    return containers[0].get("name") == CONTAINER_NAME


def _container_fields_ok(pod):
    for container in _all_containers(pod):
        if not set(container) <= _CONTAINER_KEYS:
            return False
        if container.get("imagePullPolicy") != IMAGE_PULL_POLICY:
            return False
    return True


def _command_ok(pod):
    return all(_same(container.get("command"), list(WORKER_COMMAND)) for container in _all_containers(pod))


def _pod_name_ok(pod):
    return _valid_pod_name(_at(pod, "metadata", "name")) and _at(pod, "metadata", "namespace") == pc.WORKER_NAMESPACE


def _pod_metadata_ok(pod):
    if not isinstance(pod, dict) or set(pod) != {"apiVersion", "kind", "metadata", "spec"}:
        return False
    if pod["apiVersion"] != "v1" or pod["kind"] != "Pod" or not isinstance(pod["metadata"], dict):
        return False
    return set(pod["metadata"]) <= {"name", "namespace", "labels", "annotations"}


def _spec_fields_ok(pod):
    return set(_spec(pod)) <= _SPEC_KEYS | _SPEC_KEYS_WITH_OWN_RULE


def _service_links_ok(pod):
    return _spec(pod).get("enableServiceLinks", _MISSING) is False


_POD_CHECKS = {
    "host-users": _host_users_ok,
    "host-network": _host_network_ok,
    "host-pid": _host_pid_ok,
    "host-ipc": _host_ipc_ok,
    "node-name": _node_name_ok,
    "host-path": _host_path_ok,
    "privileged": _privileged_ok,
    "capabilities": _capabilities_ok,
    "run-as": _run_as_ok,
    "seccomp": _seccomp_ok,
    "proc-mount": _proc_mount_ok,
    "security-context-keys": _security_context_keys_ok,
    "service-account": _service_account_ok,
    "automount": _automount_ok,
    "volumes": _volumes_ok,
    "volume-mounts": _volume_mounts_ok,
    "env-names": _env_names_ok,
    "env-source": _env_source_ok,
    "env-collect": _env_collect_ok,
    "node-selector": _node_selector_ok,
    "tolerations": _tolerations_ok,
    "anti-affinity": _anti_affinity_ok,
    "restart-policy": _restart_policy_ok,
    "deadline": _deadline_ok,
    "grace": _grace_ok,
    "resources": _resources_ok,
    "labels": _labels_ok,
    "annotations": _annotations_ok,
    "identity-label": _identity_label_ok,
    "owner-references": _owner_references_ok,
    "containers": _containers_ok,
    "container-fields": _container_fields_ok,
    "command": _command_ok,
    "pod-name": _pod_name_ok,
    "pod-metadata": _pod_metadata_ok,
    "spec-fields": _spec_fields_ok,
    "service-links": _service_links_ok,
}


def _complies(check, *arguments):
    try:
        return bool(check(*arguments))
    except Exception:  # noqa: BLE001 - fail closed on anything (a RecursionError of a deeply nested value included)
        return False  # an object the rules cannot read is refused


def check_policy(pod, allowed_image):
    """The ids of the pod rules `pod` breaks, in POD_RULES order (an empty list means admitted).

    `allowed_image` is the parameter of the real policy (the pinned image reference of the
    launcher). A pod that is not a mapping breaks every rule; a rule that cannot read the pod (a
    value nested too deeply to walk, or any other failure) is broken too, and nothing is raised.
    """
    if not isinstance(pod, dict):
        return list(POD_RULES)
    broken = []
    for rule in POD_RULES:
        ok = _complies(_image_ok, pod, allowed_image) if rule == "image" else _complies(_POD_CHECKS[rule], pod)
        if not ok:
            broken.append(rule)
    return broken


# --- Secret rules ---------------------------------------------------------------------------------


def _secret_namespace_ok(secret, _pod_name):
    return _at(secret, "metadata", "namespace") == pc.WORKER_NAMESPACE


def _secret_name_ok(secret, pod_name):
    return _valid_pod_name(pod_name) and _at(secret, "metadata", "name") == pc.secret_name(pod_name)


def _secret_type_ok(secret, _pod_name):
    return _at(secret, "type") == "Opaque"


def _decodes(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        return bool(base64.b64decode(value, validate=True))
    except ValueError:
        return False


def _secret_keys_ok(secret, _pod_name):
    data = _at(secret, "data")
    if not isinstance(data, dict) or set(data) != {pc.SECRET_KEY_TOKEN, pc.SECRET_KEY_ANTHROPIC}:
        return False
    return all(_decodes(value) for value in data.values())


def _secret_owner_ok(secret, pod_name):
    owners = _at(secret, "metadata", "ownerReferences")
    if not isinstance(owners, list) or len(owners) != 1 or not isinstance(owners[0], dict):
        return False
    # An explicit false is what the API default means; a true needs a finalizer permission.
    flags = ("blockOwnerDeletion", "controller")
    owner = {key: value for key, value in owners[0].items() if not (key in flags and value is False)}
    return set(owner) == {"apiVersion", "kind", "name", "uid"} and (
        owner["apiVersion"] == "v1"
        and owner["kind"] == "Pod"
        and owner["name"] == pod_name
        and isinstance(owner["uid"], str)
        and _UID.fullmatch(owner["uid"]) is not None
    )


def _secret_fields_ok(secret, _pod_name):
    if not isinstance(secret, dict) or set(secret) != _SECRET_TOP_KEYS:
        return False
    if secret["apiVersion"] != "v1" or secret["kind"] != "Secret" or not isinstance(secret["metadata"], dict):
        return False
    return set(secret["metadata"]) == _SECRET_META_KEYS


_SECRET_CHECKS = {
    "secret-namespace": _secret_namespace_ok,
    "secret-name": _secret_name_ok,
    "secret-type": _secret_type_ok,
    "secret-keys": _secret_keys_ok,
    "secret-owner": _secret_owner_ok,
    "secret-fields": _secret_fields_ok,
}


def check_secret_policy(secret, pod_name):
    """The ids of the Secret rules `secret` breaks for the pod `pod_name`, in SECRET_RULES order.

    Like `check_policy`, it never raises: a rule that cannot read the object is broken.
    """
    if not isinstance(secret, dict):
        return list(SECRET_RULES)
    return [rule for rule in SECRET_RULES if not _complies(_SECRET_CHECKS[rule], secret, pod_name)]


# --- JSON patch (add, remove, replace) ---------------------------------------------------------------


def _tokens(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("invalid patch path")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _index(token, size, allow_end):
    if token == "-" and allow_end:
        return size
    if not token.isdigit() or int(token) > size or (int(token) == size and not allow_end):
        raise ValueError("invalid patch path")
    return int(token)


def _walk(document, tokens):
    current = document
    for token in tokens:
        if isinstance(current, list):
            current = current[_index(token, len(current), False)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError("invalid patch path")
    return current


def _write(parent, token, operation, value):
    if isinstance(parent, list):
        position = _index(token, len(parent), operation == "add")
        if operation == "add":
            parent.insert(position, value)
        elif operation == "replace":
            parent[position] = value
        else:
            del parent[position]
        return
    if not isinstance(parent, dict) or (operation != "add" and token not in parent):
        raise ValueError("invalid patch path")
    if operation == "remove":
        del parent[token]
    else:
        parent[token] = value


def apply_patch(document, patch):
    """A deep copy of `document` with the operations of `patch` (add, remove, replace) applied.

    Paths are JSON pointers. A bad path, an unknown operation or an operation on the root is a
    ValueError; the input is never changed.
    """
    result = copy.deepcopy(document)
    for step in patch:
        if not isinstance(step, dict) or step.get("op") not in ("add", "remove", "replace"):
            raise ValueError("invalid patch operation")
        tokens = _tokens(step.get("path"))
        value = copy.deepcopy(step.get("value"))
        _write(_walk(result, tokens[:-1]), tokens[-1], step["op"], value)
    return result
