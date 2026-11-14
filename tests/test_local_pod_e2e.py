"""The renderer and the stub of tools/local_pod_e2e (docs/pod-backend-local-e2e.md), pinned against the repository's own definitions.

The harness itself needs docker, kind and a kernel that runs pods, so it is not run here. What IS pinned: that the hand translation of the
Terraform objects (``render.py``) names, for the manager stand-in, the launcher and the relay exactly what ``factory.podcontract`` and
``infra/pod_backend.tf`` say; that its NetworkPolicy has the three egress rules of ``infra/worker_ns.tf`` with the same excluded ranges; that
its Role has the verbs of ``infra/worker_rbac.tf`` and no others; that the stub agent's transcript is a valid stream-json result for every phase
schema it answers; and that the stub's modes are the ones the harness asks for. Nothing here starts a process but the stub, or touches a cluster.
"""

import ast
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from factory import podcontract as pc
from factory import schemas

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "tools" / "local_pod_e2e"
IMAGE = "registry.example.invalid/factory@sha256:" + "0" * 64


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render = load("render.py")


class Args:
    image = IMAGE
    pod_cidr = "10.244.0.0/16"
    service_cidr = "10.96.0.0/16"
    node_cidr = "172.31.0.0/24"
    api_public_host = "172.31.1.10"
    max_sessions = 2
    placeholder_key = "placeholder"


def items(part):
    return render.PARTS[part](Args)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.deployment = items("manager")[-1]
        self.spec = self.deployment["spec"]["template"]["spec"]
        self.containers = {c["name"]: c for c in self.spec["containers"]}

    def test_the_three_containers_and_the_init_container_of_pod_backend_tf(self):
        self.assertEqual(set(self.containers), {"factory", "launcher", "relay"})
        self.assertEqual([c["name"] for c in self.spec["initContainers"]], ["volume-perms"])
        self.assertIs(self.spec["hostUsers"], False)
        self.assertIs(self.spec["automountServiceAccountToken"], False)
        self.assertIs(self.spec["enableServiceLinks"], False)

    def test_only_the_launcher_mounts_the_token_and_the_sidecars_have_the_contracts_ids(self):
        mounts = {name: {m["name"] for m in c["volumeMounts"]} for name, c in self.containers.items()}
        self.assertIn("launcher-token", mounts["launcher"])
        self.assertTrue(all("launcher-token" not in mounts[n] for n in ("factory", "relay")))
        self.assertEqual(self.containers["launcher"]["securityContext"]["runAsUser"], pc.LAUNCHER_UID)
        self.assertEqual(self.containers["relay"]["securityContext"]["runAsUser"], pc.RELAY_UID)
        for name in ("launcher", "relay"):
            context = self.containers[name]["securityContext"]
            self.assertEqual(context["runAsGroup"], pc.SHARED_GID)
            self.assertIs(context["readOnlyRootFilesystem"], True)
            self.assertEqual(context["capabilities"], {"drop": ["ALL"]})
        socket_mount = next(m for m in self.containers["factory"]["volumeMounts"] if m["name"] == "launcher-socket")
        self.assertIs(socket_mount["readOnly"], True)

    def test_every_environment_name_is_the_contracts_and_the_launcher_has_all_of_them(self):
        launcher_env = {e["name"] for e in self.containers["launcher"]["env"]}
        self.assertTrue(set(pc.ENV_LAUNCHER.values()) <= launcher_env, set(pc.ENV_LAUNCHER.values()) - launcher_env)
        self.assertIn("ANTHROPIC_API_KEY", launcher_env)
        relay_env = {e["name"]: e["value"] for e in self.containers["relay"]["env"]}
        self.assertEqual(relay_env[pc.ENV_RELAY["port"]], str(pc.RELAY_PORT))
        self.assertEqual(relay_env[pc.ENV_RELAY["spool_dir"]], pc.SPOOL_DIR)
        main_env = {e["name"]: e["value"] for e in self.containers["factory"]["env"]}
        self.assertEqual(main_env[pc.ENV_MANAGER["pod_backend"]], pc.POD_BACKEND_ON)
        self.assertEqual(main_env[pc.ENV_MANAGER["max_pod_sessions"]], "2")
        launcher = {e["name"]: e.get("value") for e in self.containers["launcher"]["env"]}
        self.assertEqual(launcher[pc.ENV_LAUNCHER["image"]], IMAGE)
        self.assertEqual(launcher[pc.ENV_LAUNCHER["allowed_uid"]], str(pc.SUPERVISOR_UID))
        self.assertEqual(launcher[pc.ENV_LAUNCHER["relay_url"]], f"http://factory-relay-svc.factory.svc.cluster.local:{pc.RELAY_PORT}")

    def test_the_init_command_is_the_one_of_pod_backend_tf(self):
        text = self.spec["initContainers"][0]["command"][2]
        self.assertEqual(
            text.split(" && "),
            [
                f"chown {pc.LAUNCHER_UID}:{pc.SHARED_GID} {pc.LAUNCHER_SOCKET_DIR}",
                f"chmod 0750 {pc.LAUNCHER_SOCKET_DIR}",
                f"chown {pc.RELAY_UID}:{pc.SHARED_GID} {pc.SPOOL_DIR}",
                f"chmod 2770 {pc.SPOOL_DIR}",
            ],
        )
        tf = (ROOT / "infra" / "pod_backend.tf").read_text()
        for needle in ("chmod 0750", "chmod 2770", "CHOWN", "FOWNER", "FSETID"):
            self.assertIn(needle, tf)

    def test_the_main_container_has_the_three_relaxations_and_the_stand_in_command_listens_on_the_two_ports(self):
        main = self.containers["factory"]
        self.assertEqual(main["securityContext"], {"procMount": "Unmasked", "appArmorProfile": {"type": "Unconfined"}})
        code = main["args"][-1]
        self.assertIn(str(pc.APP_PORT), code)
        self.assertIn(str(pc.METRICS_PORT), code)
        self.assertNotIn("command", main, "the image's entrypoint must stay: it drops to the supervisor identity")

    def test_the_services_select_the_manager_pod_and_the_relay_one_is_not_named_factory_relay(self):
        services = {i["metadata"]["name"]: i for i in items("manager") if i["kind"] == "Service"}
        self.assertEqual(set(services), {"factory", "factory-metrics", "factory-relay-svc"})
        for service in services.values():
            self.assertEqual(service["spec"]["selector"], render.SELECTOR)


class FenceTest(unittest.TestCase):
    def test_the_network_policy_has_the_three_egress_rules_and_the_excluded_ranges(self):
        policy = items("netpol")[0]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertNotIn("ingress", policy)
        dns, relay, internet = policy["egress"]
        self.assertEqual({p["port"] for p in dns["ports"]}, {53})
        self.assertEqual(relay["ports"], [{"port": pc.RELAY_PORT, "protocol": "TCP"}])
        self.assertEqual(internet["ports"], [{"port": 443, "protocol": "TCP"}])
        block = internet["to"][0]["ipBlock"]
        self.assertEqual(block["cidr"], "0.0.0.0/0")
        self.assertEqual(block["except"][:2], ["169.254.0.0/16", "168.63.129.16/32"])
        self.assertEqual(len(block["except"]), 5)
        tf = (ROOT / "infra" / "worker_ns.tf").read_text()
        for needle in ('"169.254.0.0/16"', '"168.63.129.16/32"', "var.aks_pod_cidr", "var.aks_service_cidr", "var.node_subnet_prefix"):
            self.assertIn(needle, tf)

    def test_the_role_has_exactly_the_verbs_of_worker_rbac_tf(self):
        role = items("rbac")[0]
        self.assertEqual([(r["resources"], r["verbs"]) for r in role["rules"]], [(["pods"], ["create", "get", "list", "delete"]), (["secrets"], ["create", "delete"])])
        tf = (ROOT / "infra" / "worker_rbac.tf").read_text()
        self.assertIn('verbs      = ["create", "get", "list", "delete"]', tf)
        self.assertIn('verbs      = ["create", "delete"]', tf)
        binding = items("rbac")[1]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "factory", "namespace": pc.MANAGER_NAMESPACE}])

    def test_the_namespaces_and_the_parameter_map(self):
        names = {i["metadata"]["name"] for i in items("namespaces") if i["kind"] == "Namespace"}
        self.assertEqual(names, {pc.MANAGER_NAMESPACE, pc.WORKER_NAMESPACE})
        worker = next(i for i in items("namespaces") if i["metadata"]["name"] == pc.WORKER_NAMESPACE and i["kind"] == "Namespace")
        self.assertEqual(worker["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "privileged")
        self.assertEqual(items("params")[0]["data"], {"image": IMAGE})
        self.assertTrue(re.fullmatch(pc.IMAGE_REFERENCE_PATTERN, IMAGE))


class StubTest(unittest.TestCase):
    def run_stub(self, prompt, cwd):
        return subprocess.run([sys.executable, str(HERE / "stub-claude")], input=prompt, capture_output=True, text=True, cwd=cwd, timeout=60)

    def test_ok_writes_a_stream_json_result_that_satisfies_the_phase_schema_and_edits_the_tree(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_stub("STUB mode=ok\ntask\n", tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            events = [json.loads(line) for line in done.stdout.splitlines()]
            self.assertEqual([e["type"] for e in events], ["system", "assistant", "result"])
            result = events[-1]
            self.assertEqual((result["subtype"], result["is_error"]), ("success", False))
            for key in schemas.BUILDER["required"]:
                self.assertIn(key, result["structured_output"])
            self.assertEqual((Path(tmp) / "hello.txt").read_text(), "hello from the pod\n")

    def test_review_and_failure_modes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            review = json.loads(self.run_stub("STUB mode=noedit\n", tmp).stdout.splitlines()[-1])
            for key in schemas.REVIEWER["required"]:
                self.assertIn(key, review["structured_output"])
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertEqual(self.run_stub("STUB mode=exit code=3\n", tmp).returncode, 3)
            self.assertNotIn('"result"', self.run_stub("STUB mode=noresult\n", tmp).stdout.split("assistant")[-1].split("\n", 1)[-1])

    def test_probe_writes_booleans_and_names_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_stub("STUB mode=probe\n", tmp).returncode, 0)
            document = json.loads((Path(tmp) / "probe-result.json").read_text())
            self.assertTrue(all(isinstance(v, bool) for v in document["checks"].values()))
            self.assertIsInstance(document["env_names"], list)
            self.assertNotIn("env", {k for k in document if k != "env_names"} - {"env_count", "env_names", "env_names_with_secret_shape"})

    def test_every_mode_the_harness_asks_for_exists_in_the_stub(self):
        stub = (HERE / "stub-claude").read_text()
        modes = set(re.findall(r"mode=([a-z-]+)", (HERE / "harness.py").read_text()))
        for mode in modes:
            self.assertIn(f'"{mode}"', stub, mode)


class HarnessSourceTest(unittest.TestCase):
    def test_every_file_parses_and_the_harness_never_names_the_users_kubeconfig(self):
        for path in (HERE / "harness.py", HERE / "driver.py", HERE / "render.py", HERE / "launcher_probe.py", HERE / "stub-claude"):
            ast.parse(path.read_text(), str(path))
        harness = (HERE / "harness.py").read_text()
        self.assertNotIn("expanduser", harness)
        self.assertNotIn("Path.home", harness)
        self.assertIn("--kubeconfig", harness)
        # Every kubectl call goes through Cluster.kubectl, which always carries --kubeconfig.
        self.assertEqual(len(re.findall(r'\["kubectl"', harness)), 1)

    def test_no_secret_value_is_a_constant_of_the_tools(self):
        for path in HERE.iterdir():
            if path.is_file():
                text = path.read_text()
                self.assertNotRegex(text, r"sk-ant-api")
                self.assertNotRegex(text, r"(?<![A-Za-z0-9/_.:@=-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{64,}(?![A-Za-z0-9/_.-])")


if __name__ == "__main__":
    unittest.main()
