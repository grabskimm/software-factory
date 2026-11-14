"""The worker pod contract (src/factory/podcontract.py): names, numbers and the pure helpers.

Slice 0 of the pod backend (docs/pod-backend.md). Nothing here touches Kubernetes, the network or
a process; the numbers pinned below are the PROVISIONAL ones of the design, so changing one is a
deliberate edit of this file, of podcontract.py and of the Terraform local worker_contract together.
infra/test/test_pod_contract.py compares that local with SHARED; this file checks the contract
itself and the module's discipline.
"""

import ast
import hashlib
import math
import re
import sys
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from factory import phases
from factory import podcontract as pc

SOURCE = Path(pc.__file__).resolve()
ROOT = SOURCE.parents[2]

SESSION = "0123456789abcdef0123456789abcdef"
INSTANCE = "abcdef012345"
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
GIB = 1024**3
# Allocatable vCPUs of a D4ds_v5 node (measured on a throwaway NAP node in the desk research).
NODE_ALLOCATABLE_CPU = Fraction(386, 100)


def sha12(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def assignments():
    """Module-level UPPER_CASE assignments of podcontract.py: name -> value expression."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper() and not name.startswith("_"):
                found[name] = node.value
    return found


def reads_from_shared(expression):
    if not isinstance(expression, ast.Subscript):
        return False
    while isinstance(expression, ast.Subscript):
        expression = expression.value
    return isinstance(expression, ast.Name) and expression.id == "SHARED"


class FullMatch(unittest.TestCase):
    def assertFullMatch(self, pattern, text):
        self.assertIsNotNone(re.fullmatch(pattern, text), f"{text!r} does not match {pattern.pattern if hasattr(pattern, 'pattern') else pattern}")


def env_names():
    """Every environment variable name of the contract's env section, in order."""
    names = []
    for group in pc.SHARED["env"].values():
        names.extend(group.values() if isinstance(group, dict) else group)
    return names


class Names(FullMatch):
    def test_the_namespaces_and_object_names_are_kubernetes_names(self):
        for name in (
            pc.MANAGER_NAMESPACE,
            pc.WORKER_NAMESPACE,
            pc.WORKER_SERVICE_ACCOUNT,
            pc.NODEPOOL_NAME,
            pc.NODECLASS_NAME,
        ):
            with self.subTest(name=name):
                self.assertLessEqual(len(name), 63)
                self.assertFullMatch(DNS_LABEL, name)
        self.assertNotEqual(pc.WORKER_NAMESPACE, pc.MANAGER_NAMESPACE)
        self.assertEqual((pc.MANAGER_NAMESPACE, pc.WORKER_NAMESPACE), ("factory", "factory-workers"))

    def test_the_pod_and_secret_names_fit_a_dns_label(self):
        pod = pc.pod_name(SESSION)
        self.assertEqual(pod, f"factory-w-{SESSION}")
        self.assertEqual(pc.secret_name(pod), f"{pod}-s")
        for name in (pod, pc.secret_name(pod)):
            with self.subTest(name=name):
                self.assertLessEqual(len(name), 63)
                self.assertFullMatch(DNS_LABEL, name)

    def test_the_label_and_annotation_keys_are_the_ones_of_the_design(self):
        self.assertEqual(pc.LABEL_PREFIX, "factory.internal/")
        self.assertEqual(
            pc.LABEL_KEYS, ["role", "repo", "run", "job", "attempt", "phase", "manager", "session"]
        )
        self.assertEqual(pc.ANNOTATION_KEYS, ["deadline", "repo", "run-id", "job-id"])
        self.assertEqual(pc.ANNOTATION_DO_NOT_DISRUPT, "karpenter.sh/do-not-disrupt")
        for key in [*(pc.label_key(k) for k in pc.LABEL_KEYS), *(pc.annotation_key(k) for k in pc.ANNOTATION_KEYS)]:
            with self.subTest(key=key):
                prefix, _, name = key.partition("/")
                self.assertEqual(f"{prefix}/", pc.LABEL_PREFIX)
                self.assertFullMatch(r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?", name)
                self.assertLessEqual(len(name), 63)

    def test_an_unknown_label_or_annotation_key_is_refused(self):
        for helper in (pc.label_key, pc.annotation_key):
            for bad in ("", "other", "role ", "deadline ", None):
                with self.subTest(helper=helper.__name__, bad=bad), self.assertRaises(ValueError):
                    helper(bad)
        # A label key is not an annotation key and the other way round.
        with self.assertRaises(ValueError):
            pc.label_key("job-id")
        with self.assertRaises(ValueError):
            pc.annotation_key("role")

    def test_the_phases_are_the_phases_of_the_package(self):
        self.assertEqual(tuple(pc.PHASES), phases.PHASES)

    def test_the_node_label_and_the_two_taints(self):
        self.assertEqual((pc.WORKER_NODE_LABEL_KEY, pc.WORKER_NODE_LABEL_VALUE), ("pool", "worker"))
        self.assertEqual((pc.WORKER_TAINT_KEY, pc.WORKER_TAINT_VALUE), ("workload", "worker"))
        self.assertEqual((pc.MANAGER_TAINT_KEY, pc.MANAGER_TAINT_VALUE), ("workload", "manager"))
        self.assertEqual(pc.TAINT_EFFECT, "NoSchedule")
        self.assertNotEqual(pc.WORKER_TAINT_VALUE, pc.MANAGER_TAINT_VALUE)

    def test_the_secret_keys_and_the_switch_values(self):
        self.assertEqual((pc.SECRET_KEY_TOKEN, pc.SECRET_KEY_ANTHROPIC), ("token", "anthropic-api-key"))
        self.assertEqual((pc.POD_BACKEND_ON, pc.POD_BACKEND_OFF), ("on", "off"))

    def test_the_image_reference_pattern_accepts_a_digest_and_nothing_else(self):
        pattern = re.compile(pc.IMAGE_REFERENCE_PATTERN)
        digest = "0123456789abcdef" * 4
        for good in (
            f"registry.example.invalid/factory@sha256:{digest}",
            f"registry.example:5000/team/factory@sha256:{digest}",
        ):
            with self.subTest(good=good):
                self.assertIsNotNone(pattern.fullmatch(good))
        for bad in (
            "registry.example.invalid/factory:latest",
            "registry.example.invalid/factory",
            f"registry.example.invalid/factory@sha256:{digest[:-1]}",
            f"registry.example.invalid/factory@sha256:{digest}0",
            f"registry.example.invalid/factory@sha256:{digest.upper()}",
            f"registry.example.invalid/factory@sha512:{digest}",
            f"factory@sha256:{digest}",
            f"Registry.example.invalid/factory@sha256:{digest}",
            f"registry.example.invalid/factory@sha256:{digest}\n",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.fullmatch(bad))


class PortsAndIdentities(unittest.TestCase):
    def test_no_two_ports_clash(self):
        ports = pc.SHARED["ports"]
        self.assertEqual(ports, {"relay": 8090, "app": 8080, "metrics": 9464})
        self.assertEqual(len(set(ports.values())), len(ports))
        for port in ports.values():
            self.assertTrue(1024 <= port <= 65535)

    def test_the_paths_are_absolute_and_distinct(self):
        paths = pc.SHARED["paths"]
        self.assertEqual(len(set(paths.values())), len(paths))
        for name, path in paths.items():
            with self.subTest(name=name):
                self.assertTrue(path.startswith("/"))
                self.assertNotIn("..", path.split("/"))
        self.assertEqual(Path(pc.LAUNCHER_SOCKET).parent, Path(pc.LAUNCHER_SOCKET_DIR))
        self.assertEqual(pc.SESSION_SECRET_DIR, "/run/factory-session")
        self.assertEqual((pc.SCRATCH_DIR, pc.TMP_DIR, pc.SPOOL_DIR), ("/data", "/tmp", "/spool"))

    def test_the_sidecar_identities_do_not_clash_with_the_image_users(self):
        taken = {pc.SUPERVISOR_UID, pc.WORKER_UID, pc.WORKER_GID}
        self.assertEqual(taken, {10001, 10002, 10010})
        self.assertNotEqual(pc.LAUNCHER_UID, pc.RELAY_UID)
        for uid in (pc.LAUNCHER_UID, pc.RELAY_UID):
            with self.subTest(uid=uid):
                self.assertNotIn(uid, taken)
                self.assertGreater(uid, 10000)
        # The primary group of both sidecars, of the socket directory and of the spool is the supervisor's.
        self.assertEqual(pc.SHARED_GID, 10001)
        self.assertNotEqual(pc.SHARED_GID, pc.WORKER_GID)

    def test_the_image_users_are_the_ones_the_dockerfile_creates(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, rf"useradd [^\n]*--uid {pc.SUPERVISOR_UID} --gid {pc.SHARED_GID}\b")
        self.assertRegex(dockerfile, rf"useradd [^\n]*--uid {pc.WORKER_UID} --gid {pc.WORKER_GID}\b")
        self.assertRegex(dockerfile, rf"groupadd --gid {pc.WORKER_GID} ")


class EnvironmentNames(FullMatch):
    def test_every_name_appears_exactly_once(self):
        names = env_names()
        self.assertEqual(len(names), len(set(names)), sorted(n for n in names if names.count(n) > 1))
        # Manager 4, launcher 11, relay 2 and worker pod 9 names.
        self.assertEqual(len(names), 28)
        for name in names:
            with self.subTest(name=name):
                self.assertFullMatch(r"[A-Z][A-Z0-9_]*", name)

    def test_the_manager_instance_variable_is_python_only_and_not_a_terraform_name(self):
        self.assertEqual(pc.MANAGER_INSTANCE_ENV, "FACTORY_MANAGER_INSTANCE")
        self.assertNotIn(pc.MANAGER_INSTANCE_ENV, env_names())
        self.assertIn("MANAGER_INSTANCE_ENV", pc.PYTHON_ONLY)

    def test_the_names_of_the_three_programs_use_their_own_prefixes(self):
        self.assertEqual(
            sorted(pc.ENV_MANAGER),
            ["launcher_socket", "max_pod_sessions", "pod_backend", "spool_dir"],
        )
        self.assertEqual(pc.ENV_MANAGER["pod_backend"], "FACTORY_POD_BACKEND")
        self.assertEqual(pc.ENV_MANAGER["max_pod_sessions"], "FACTORY_MAX_POD_SESSIONS")
        for name in pc.ENV_LAUNCHER.values():
            self.assertTrue(name.startswith("FACTORY_LAUNCHER_"), name)
        for name in pc.ENV_RELAY.values():
            self.assertTrue(name.startswith("FACTORY_RELAY_"), name)
        # docs/manager.md documents every FACTORY_MANAGER_* name the manager itself reads.
        for name in pc.ENV_MANAGER.values():
            self.assertFalse(name.startswith("FACTORY_MANAGER_"), name)

    def test_the_pod_allow_list_holds_no_secret_like_name_and_no_cloud_name(self):
        self.assertEqual(
            pc.ENV_POD,
            [
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
        )
        for name in pc.ENV_POD:
            with self.subTest(name=name):
                self.assertIsNone(re.search(r"KEY|TOKEN|SECRET|PASSWORD", name))
                self.assertFalse(name.startswith("AZURE_"))


class Resources(unittest.TestCase):
    def test_the_class_table_is_the_one_of_the_design(self):
        self.assertEqual(pc.DEFAULT_CLASS, "standard")
        self.assertEqual(sorted(pc.CLASSES), ["small", "standard"])
        self.assertEqual(
            pc.CLASSES["standard"],
            {
                "cpu": "2",
                "memory": "4Gi",
                "ephemeral_storage": "20Gi",
                "pods_per_node": 1,
                "data_size_limit": "14Gi",
                "tmp_size_limit": "2Gi",
            },
        )
        self.assertEqual(
            pc.CLASSES["small"],
            {
                "cpu": "1",
                "memory": "2Gi",
                "ephemeral_storage": "10Gi",
                "pods_per_node": 3,
                "data_size_limit": "7Gi",
                "tmp_size_limit": "1Gi",
            },
        )

    def test_sizes_parse_as_quantities_and_the_scratch_volumes_leave_room_for_the_layer(self):
        for name, size in pc.CLASSES.items():
            with self.subTest(name=name):
                ephemeral = pc.parse_quantity(size["ephemeral_storage"])
                scratch = sum(pc.parse_quantity(size[key]) for key in ("data_size_limit", "tmp_size_limit"))
                self.assertGreater(pc.parse_quantity(size["cpu"]), 0)
                self.assertGreater(pc.parse_quantity(size["memory"]), 0)
                self.assertGreater(scratch, 0)
                # The writable layer and the logs need at least 2Gi of the ephemeral-storage limit.
                self.assertGreaterEqual(ephemeral - scratch, 2 * GIB)

    def test_pods_per_node_is_what_a_node_holds(self):
        for name, size in pc.CLASSES.items():
            with self.subTest(name=name):
                self.assertEqual(size["pods_per_node"], math.floor(NODE_ALLOCATABLE_CPU / pc.parse_quantity(size["cpu"])))

    def test_the_default_class_exists(self):
        self.assertIn(pc.DEFAULT_CLASS, pc.CLASSES)

    def test_the_spool_is_refused_before_its_size_limit_can_evict_the_manager_pod(self):
        limit = pc.parse_quantity(pc.SPOOL_SIZE_LIMIT)
        self.assertEqual(pc.SPOOL_SIZE_LIMIT, "16Gi")
        self.assertLessEqual(pc.SPOOL_REFUSE_BYTES, limit * Fraction(3, 4))
        self.assertGreater(pc.SPOOL_REFUSE_BYTES, 0)
        self.assertEqual(pc.SOCKET_DIR_SIZE_LIMIT, "1Mi")

    def test_every_transfer_cap_fits_in_the_spool_with_room_to_spare(self):
        # A transfer that is accepted must be able to finish before the refusal threshold.
        for cap in (pc.BUNDLE_MAX_BYTES, pc.DELTA_MAX_BYTES, pc.TRANSCRIPT_MAX_BYTES, pc.STDERR_MAX_BYTES):
            with self.subTest(cap=cap):
                self.assertLess(cap, pc.SPOOL_REFUSE_BYTES)


class Canary(unittest.TestCase):
    def test_the_forbidden_destinations_are_the_closed_list_of_the_design(self):
        self.assertEqual(
            pc.CANARY_FORBIDDEN,
            [
                {"host": "169.254.169.254", "port": 80},
                {"host": "168.63.129.16", "port": 80},
                {"host": "168.63.129.16", "port": 32526},
                {"host_env": "FACTORY_NODE_IP", "port": 10250},
                {"host_env": "FACTORY_MANAGER_ADDRESS", "port": pc.APP_PORT},
                {"host_env": "FACTORY_MANAGER_METRICS_ADDRESS", "port": pc.METRICS_PORT},
            ],
        )

    def test_the_two_api_probes_and_what_counts_as_a_denial(self):
        self.assertEqual(
            pc.CANARY_API_PROBES,
            [
                {"name": "service", "host_env": "KUBERNETES_SERVICE_HOST", "port_env": "KUBERNETES_SERVICE_PORT"},
                {"name": "public", "host_env": "FACTORY_API_PUBLIC_HOST", "port": 443},
            ],
        )
        self.assertEqual(pc.CANARY_API_DENIED_STATUSES, [401, 403])
        self.assertTrue(pc.CANARY_API_PROBE_PATH.startswith(f"/api/v1/namespaces/{pc.MANAGER_NAMESPACE}/"))

    def test_every_environment_name_the_canary_reads_is_in_the_pod_or_injected_by_the_kubelet(self):
        injected = {"KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"}
        names = {entry["host_env"] for entry in [*pc.CANARY_FORBIDDEN, *pc.CANARY_API_PROBES] if "host_env" in entry}
        names |= {entry["port_env"] for entry in pc.CANARY_API_PROBES if "port_env" in entry}
        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertIn(name, set(pc.ENV_POD) | injected)

    def test_the_relay_port_the_control_connect_needs_is_not_a_forbidden_port(self):
        self.assertNotIn(pc.RELAY_PORT, {entry["port"] for entry in pc.CANARY_FORBIDDEN})

    def test_every_forbidden_entry_names_exactly_one_kind_of_host(self):
        for entry in pc.CANARY_FORBIDDEN:
            with self.subTest(entry=entry):
                self.assertEqual(len({"host", "host_env"} & set(entry)), 1)
                self.assertIn("port", entry)


class ProvisionalNumbers(unittest.TestCase):
    def test_the_numbers_are_those_of_the_design(self):
        self.assertEqual(
            (pc.DEADLINE_CLAMP_SECONDS, pc.DEADLINE_MARGIN_SECONDS, pc.TERMINATION_GRACE_SECONDS, pc.PER_REPO_CAP),
            (14400, 300, 30, 4),
        )
        self.assertEqual(
            (
                pc.HEARTBEAT_SECONDS,
                pc.ORPHAN_SILENCE_SECONDS,
                pc.SCHEDULING_TIMEOUT_SECONDS,
                pc.PULL_ERROR_SECONDS,
                pc.LAUNCH_GRACE_SECONDS,
                pc.ORPHAN_SWEEP_SECONDS,
                pc.SPOOL_RETENTION_SECONDS,
            ),
            (30, 300, 600, 120, 120, 120, 3600),
        )
        self.assertEqual(
            (
                pc.BUNDLE_MAX_BYTES,
                pc.DELTA_MAX_BYTES,
                pc.DELTA_MAX_FILES,
                pc.FILE_MAX_BYTES,
                pc.PATH_MAX_BYTES,
                pc.TRANSCRIPT_MAX_BYTES,
                pc.STDERR_MAX_BYTES,
            ),
            (GIB, 256 * 1024**2, 20000, 64 * 1024**2, 4096, 512 * 1024**2, 16 * 1024**2),
        )
        self.assertEqual(
            (
                pc.RELAY_IDLE_TIMEOUT_SECONDS,
                pc.RELAY_STREAM_TOTAL_SECONDS,
                pc.RELAY_TRANSFER_TOTAL_SECONDS,
                pc.RELAY_MAX_THREADS,
            ),
            (60, 120, 900, 32),
        )
        self.assertEqual((pc.PACING_BURST, pc.PACING_INTERVAL_SECONDS), (2, 15))
        self.assertEqual(
            (pc.KUBE_CONNECT_TIMEOUT_SECONDS, pc.KUBE_READ_TIMEOUT_SECONDS, pc.KUBE_RETRIES, pc.KUBE_RETRY_BACKOFF_SECONDS),
            (5, 30, 3, (1, 2, 4)),
        )
        self.assertEqual(len(pc.KUBE_RETRY_BACKOFF_SECONDS), pc.KUBE_RETRIES)

    def test_the_termination_grace_is_the_limit_the_admission_policy_pins(self):
        self.assertLessEqual(pc.TERMINATION_GRACE_SECONDS, 30)

    def test_a_request_is_cut_off_after_it_has_been_idle_and_a_transfer_may_take_longer_than_a_chunk(self):
        self.assertLess(pc.RELAY_IDLE_TIMEOUT_SECONDS, pc.RELAY_STREAM_TOTAL_SECONDS)
        self.assertLess(pc.RELAY_STREAM_TOTAL_SECONDS, pc.RELAY_TRANSFER_TOTAL_SECONDS)

    def test_the_deadline_is_the_clamped_timeout_plus_the_margin(self):
        self.assertEqual(pc.active_deadline_seconds(1800), 2100)
        self.assertEqual(pc.active_deadline_seconds(1), 301)
        self.assertEqual(pc.active_deadline_seconds(pc.DEADLINE_CLAMP_SECONDS), 14700)
        # A configured timeout above the clamp is reduced to it.
        self.assertEqual(pc.active_deadline_seconds(86400), 14700)
        self.assertEqual(pc.active_deadline_seconds(14401), 14700)
        for bad in (0, -5, 1.5, "60", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.active_deadline_seconds(bad)


class ModuleDiscipline(unittest.TestCase):
    def test_it_imports_only_the_standard_library_and_nothing_relative(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "a relative import cannot be loaded by path")
                imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported)
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"__future__"}, imported)
        self.assertNotIn("factory", imported)

    def test_a_constant_is_either_read_from_the_shared_tree_or_python_only(self):
        found = assignments()
        python_only = set(pc.PYTHON_ONLY)
        self.assertEqual(len(python_only), len(pc.PYTHON_ONLY), "a name is listed twice")
        for name, expression in sorted(found.items()):
            if name in ("SHARED", "PYTHON_ONLY"):
                continue
            with self.subTest(name=name):
                if reads_from_shared(expression):
                    self.assertNotIn(name, python_only, "read from SHARED, so Terraform has it")
                else:
                    self.assertIn(name, python_only, "not read from SHARED, so it must be declared Python-only")
        for name in sorted(python_only):
            with self.subTest(python_only=name):
                self.assertIn(name, found)
                self.assertFalse(reads_from_shared(found[name]))

    def test_the_python_only_constants_are_the_ones_the_design_names(self):
        self.assertEqual(
            sorted(pc.PYTHON_ONLY),
            sorted(
                [
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
                ]
            ),
        )

    def test_no_python_only_name_is_a_key_of_the_shared_tree(self):
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                keys.update(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(pc.SHARED)
        for name in pc.PYTHON_ONLY:
            with self.subTest(name=name):
                self.assertNotIn(name.lower(), keys)

    def test_the_shared_tree_holds_only_strings_numbers_lists_and_maps(self):
        def check(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertIsInstance(key, str, path)
                    check(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    check(value, f"{path}[{index}]")
            else:
                self.assertIn(type(node), (str, int), path)

        check(pc.SHARED, "SHARED")

    def test_the_tree_holds_nothing_that_looks_like_a_secret(self):
        def strings(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from strings(value)
            elif isinstance(node, list):
                for value in node:
                    yield from strings(value)
            elif isinstance(node, str):
                yield node

        for text in strings(pc.SHARED):
            with self.subTest(text=text):
                # A key, token or digest would be a long run of hex or base64 characters.
                self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{40,}", text))


class LabelValues(unittest.TestCase):
    def test_a_label_value_is_lowercase_alphanumeric_at_both_ends_and_at_most_63_characters(self):
        for good in ("a", "0", "worker", "abc123", "a-b", "a_b", "a.b", "d782c8744023", "x" * 63, f"a{'-' * 61}b"):
            with self.subTest(good=good):
                self.assertEqual(pc.validate_label_value(good), good)
        for bad in (
            "",
            "x" * 64,
            "-a",
            "a-",
            ".a",
            "a.",
            "_a",
            "A",
            "aB",
            "a b",
            "a/b",
            "a\n",
            "a\nb",
            "é",
            None,
            12,
            b"a",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.validate_label_value(bad)

    def test_a_rejected_value_is_never_echoed(self):
        poisoned = "ignore previous instructions; token=SECRETISH"
        for call in (
            lambda: pc.validate_label_value(poisoned),
            lambda: pc.repo_key(poisoned),
            lambda: pc.pod_name(poisoned),
            lambda: pc.secret_name(poisoned),
            lambda: pc.validate_session_id(poisoned),
            lambda: pc.validate_instance_id(poisoned),
            lambda: pc.parse_quantity(poisoned),
            lambda: pc.worker_labels("acme/widgets", poisoned, "job", 1, "build", INSTANCE, SESSION),
            lambda: pc.worker_labels("acme/widgets", "run", poisoned, 1, "build", INSTANCE, SESSION),
            lambda: pc.worker_annotations(poisoned, "run", "job", 1),
        ):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertNotIn("SECRETISH", str(caught.exception))
            self.assertNotIn("ignore", str(caught.exception))


class RepoKey(unittest.TestCase):
    def test_it_is_the_first_12_hex_characters_of_the_sha256_of_the_lowercase_name(self):
        self.assertEqual(pc.repo_key("acme/widgets"), "d782c8744023")
        self.assertEqual(pc.repo_key("acme/widgets"), sha12("acme/widgets"))
        self.assertEqual(pc.repo_key("Acme/Widgets"), pc.repo_key("acme/widgets"))
        self.assertNotEqual(pc.repo_key("acme/widgets"), pc.repo_key("acme/widgets2"))
        self.assertEqual(len(pc.repo_key("a/b")), pc.LABEL_HEX_LENGTHS["repo"])
        pc.validate_label_value(pc.repo_key("acme/widgets"))

    def test_only_an_owner_slash_name_is_accepted(self):
        for bad in ("", "acme", "acme/", "/widgets", "a/b/c", "acme/wid gets", "acme/widgets\n", "-acme/widgets", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.repo_key(bad)


class Identifiers(unittest.TestCase):
    def test_session_and_instance_ids_are_lowercase_hex_of_the_contract_length(self):
        self.assertEqual(pc.validate_session_id(SESSION), SESSION)
        self.assertEqual(pc.validate_instance_id(INSTANCE), INSTANCE)
        for bad in (SESSION[:-1], f"{SESSION}0", SESSION.upper(), f"{SESSION[:-1]}g", "", None, INSTANCE):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.validate_session_id(bad)
        for bad in (INSTANCE[:-1], f"{INSTANCE}0", INSTANCE.upper(), f"{INSTANCE[:-1]}g", "", None, SESSION):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.validate_instance_id(bad)

    def test_a_new_id_is_drawn_from_the_system_source_every_time(self):
        with mock.patch.object(pc.secrets, "token_hex", side_effect=["ab" * 6, "cd" * 6]) as draw:
            first, second = pc.new_instance_id(), pc.new_instance_id()
        self.assertEqual((first, second), ("ab" * 6, "cd" * 6))
        self.assertEqual([call.args for call in draw.call_args_list], [(6,), (6,)])
        with mock.patch.object(pc.secrets, "token_hex", return_value="ef" * 16) as draw:
            self.assertEqual(pc.new_session_id(), "ef" * 16)
        draw.assert_called_once_with(16)

    def test_a_real_draw_has_the_documented_shape(self):
        pc.validate_instance_id(pc.new_instance_id())
        pc.validate_session_id(pc.new_session_id())

    def test_the_pod_and_secret_names_refuse_anything_but_a_pod_of_the_contract(self):
        with self.assertRaises(ValueError):
            pc.pod_name("0123")
        for bad in (
            "",
            SESSION,
            f"factory-x-{SESSION}",
            f"factory-w-{SESSION[:-1]}",
            f"factory-w-{SESSION}-s",
            f"factory-w-{SESSION}\n",
            None,
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.secret_name(bad)


class Metadata(unittest.TestCase):
    def labels(self, **changes):
        arguments = {
            "repository": "Acme/Widgets",
            "run_id": "run-1",
            "job_id": "job-2",
            "attempt": 3,
            "phase": "build",
            "manager_instance": INSTANCE,
            "session_id": SESSION,
        }
        arguments.update(changes)
        return pc.worker_labels(**arguments)

    def test_the_labels_are_the_documented_digests_and_words_under_the_prefix(self):
        self.assertEqual(
            self.labels(),
            {
                "factory.internal/role": "worker",
                "factory.internal/repo": sha12("acme/widgets"),
                "factory.internal/run": "4e65d3fbe8ad",
                "factory.internal/job": "0e753487879f",
                "factory.internal/attempt": "3",
                "factory.internal/phase": "build",
                "factory.internal/manager": INSTANCE,
                "factory.internal/session": SESSION,
            },
        )
        self.assertEqual(self.labels()["factory.internal/run"], sha12("run-1"))
        self.assertEqual(self.labels()["factory.internal/job"], sha12("job-2"))
        self.assertEqual(list(self.labels()), [pc.label_key(key) for key in pc.LABEL_KEYS])

    def test_every_label_value_is_a_valid_label_value_for_every_phase(self):
        for phase in pc.PHASES:
            with self.subTest(phase=phase):
                for value in self.labels(phase=phase).values():
                    pc.validate_label_value(value)

    def test_the_labels_carry_no_readable_id_and_no_issue_text(self):
        values = set(self.labels(run_id="Run_Mixed-1").values())
        for text in ("Run_Mixed-1", "job-2", "Acme", "Widgets"):
            self.assertNotIn(text, values)

    def test_a_bad_input_is_refused(self):
        bad_cases = {
            "repository": ("acme", "", "acme/wid gets", None),
            "run_id": ("", "a b", "-run", "x" * 51, "run\n", None),
            "job_id": ("", "a b", "-job", "x" * 65, "job\n", None),
            "attempt": (-1, 10000, True, "1", 1.5, None),
            "phase": ("", "deploy", "Build", None),
            "manager_instance": (SESSION, "", INSTANCE.upper(), None),
            "session_id": (INSTANCE, "", SESSION.upper(), None),
        }
        for field, values in bad_cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.labels(**{field: value})
        self.assertEqual(self.labels(attempt=0)["factory.internal/attempt"], "0")
        self.assertEqual(self.labels(attempt=9999)["factory.internal/attempt"], "9999")

    def test_the_annotations_keep_the_readable_ids_and_the_do_not_disrupt_marker(self):
        self.assertEqual(
            pc.worker_annotations("Acme/Widgets", "run-1", "job-2", 1760000000),
            {
                "karpenter.sh/do-not-disrupt": "true",
                "factory.internal/deadline": "1760000000",
                "factory.internal/repo": "Acme/Widgets",
                "factory.internal/run-id": "run-1",
                "factory.internal/job-id": "job-2",
            },
        )
        for key in pc.worker_annotations("a/b", "r", "j", 0):
            if key != pc.ANNOTATION_DO_NOT_DISRUPT:
                self.assertTrue(key.startswith(pc.LABEL_PREFIX), key)

    def test_a_bad_annotation_input_is_refused(self):
        for arguments in (
            ("acme", "run", "job", 1),
            ("acme/widgets", "a b", "job", 1),
            ("acme/widgets", "run", "a b", 1),
            ("acme/widgets", "run", "job", -1),
            ("acme/widgets", "run", "job", 1.5),
            ("acme/widgets", "run", "job", "1"),
            ("acme/widgets", "run", "job", True),
            ("acme/widgets", "run", "job", None),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                pc.worker_annotations(*arguments)


class Quantities(unittest.TestCase):
    def test_kubernetes_quantities_are_exact(self):
        for text, expected in (
            ("2", 2),
            ("0", 0),
            ("500m", Fraction(1, 2)),
            ("1.5", Fraction(3, 2)),
            ("1k", 1000),
            ("1M", 10**6),
            ("1G", 10**9),
            ("1Ki", 1024),
            ("1Mi", 1024**2),
            ("14Gi", 14 * GIB),
            ("1.5Gi", GIB * 3 // 2),
            ("1Ti", 1024**4),
            ("1Pi", 1024**5),
            ("1Ei", 1024**6),
        ):
            with self.subTest(text=text):
                self.assertEqual(pc.parse_quantity(text), expected)

    def test_signs_exponents_spaces_and_unknown_suffixes_are_refused(self):
        for bad in ("", "-1", "+1", "1e3", "1 Gi", " 1", "1Gi ", "1gi", "1GiB", "1.", ".5", "1.5.2", "Gi", "1x", None, 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.parse_quantity(bad)


class Documentation(unittest.TestCase):
    """docs/pod-backend.md is the design record: it names every section, constant and variable."""

    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")

    def test_every_section_of_the_shared_tree_is_documented(self):
        for section in pc.SHARED:
            with self.subTest(section=section):
                self.assertIn(f"| `{section}` |", self.text)

    def test_every_public_constant_is_documented(self):
        for name in sorted(assignments()):
            if name in ("SHARED", "PYTHON_ONLY"):
                continue
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", self.text)

    def test_every_environment_variable_is_documented(self):
        for name in [*env_names(), pc.MANAGER_INSTANCE_ENV]:
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", self.text)

    def test_the_page_says_it_is_off_and_that_the_numbers_are_provisional(self):
        self.assertIn("default off, nothing is switched on", self.text)
        self.assertIn("PROVISIONAL", self.text)
        self.assertNotIn("Closes #", self.text)


if __name__ == "__main__":
    unittest.main()
