"""The environment and the directories that Terraform gives the launcher, the relay and the manager, run through the programs.

Slice 17 of the pod backend (docs/pod-backend.md, "Terraform C"). `infra/test/test_pod_backend_design.py` reads the Terraform as
text and cannot import the programs; this module can. It renders the environment entries of `infra/pod_backend.tf` with a small
evaluator (strings with interpolation, `tostring`, a path into the contract, and the handful of locals and attributes the entries
read, each given a stand-in value here), then asks the real code whether it accepts them:

  * `launcher.Config.from_environment` accepts the launcher's environment and the values come out as intended, and it REFUSES
    when any required entry is dropped (so the test would notice a missing or misspelled name);
  * `relay.check_environment` and the relay's port reader accept the relay's environment, and refuse the value Kubernetes would
    inject for a Service named `factory-relay` (the reason the Service has another name);
  * the manager's switch (`podexecution.switch_is_on`) reads `on` from the main container's environment and the launcher client
    takes its socket path from it;
  * the init container's chown and chmod produce exactly the directory that `launcher.check_directory` and
    `relay.check_root` demand.

Everything is contract-tested against what the Terraform text says; nothing here ran in a container or against a cluster.
"""

import os
import re
import stat
import unittest
from pathlib import Path

from factory import launcher, launcher_client, podcontract as pc, podexecution, relay

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"

IMAGE = "registry.example.io/factory@sha256:" + "0" * 64
FQDN = "aks-example-abcd1234.hcp.westus3.azmk8s.io"
CEILING = "8"
STAND_INS = {
    "local.image": IMAGE,
    "local.worker_pool.max_sessions": CEILING,
    "azurerm_kubernetes_cluster.this.fqdn": FQDN,
}


def tf_text(name):
    text = (INFRA / name).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def all_locals():
    """{name: single-line right-hand side} of every one-line local of infra/*.tf."""
    found = {}
    for path in sorted(INFRA.glob("*.tf")):
        for name, rhs in re.findall(r"(?m)^  (\w+)\s*=\s*(.+)$", tf_text(path.name)):
            found.setdefault(name, rhs.strip())
    return found


LOCALS = all_locals()


def evaluate(expression):
    """The string value of a Terraform expression of the small grammar of pod_backend.tf (anything else raises)."""
    expression = expression.strip().rstrip(",").strip()
    if expression in STAND_INS:
        return STAND_INS[expression]
    if expression.startswith('"') and expression.endswith('"'):
        return re.sub(r"\$\{([^}]+)\}", lambda found: evaluate(found.group(1)), expression[1:-1])
    wrapped = re.fullmatch(r"tostring\((.+)\)", expression)
    if wrapped:
        return evaluate(wrapped.group(1))
    if expression.startswith("local.worker_contract."):
        node = pc.SHARED
        for part in expression.split(".")[2:]:
            node = node[part]
        return str(node)
    named = re.fullmatch(r"local\.(\w+)", expression)
    if named and named.group(1) in LOCALS:
        return evaluate(LOCALS[named.group(1)])
    raise AssertionError(f"the evaluator does not know {expression!r}")


def local_block(name):
    """The text of the local `name` of pod_backend.tf up to the next two-space attribute."""
    text = tf_text("pod_backend.tf")
    start = re.search(rf"(?m)^  {name}\s*=\s*", text)
    rest = text[start.end() :]
    following = re.search(r"(?m)^  \w+\s*=", rest)
    return rest[: following.start()] if following else rest


def entries(text):
    """{name: value} of the one-line `{ name = X, value = Y }` entries of a block of text."""
    found = {}
    for name, value in re.findall(r"\{ name = (.+?), value = (.+?) \},?\s*$", text, re.MULTILINE):
        key = evaluate(name)
        assert key not in found, f"{key} is set twice"
        found[key] = evaluate(value)
    return found


def container(name):
    text = local_block("pod_backend_containers_all")
    marker = f"local.pod_backend_container_names.{name}"
    start = text.index(marker)
    other = [other for other in ("launcher", "relay") if other != name][0]
    following = text.find(f"local.pod_backend_container_names.{other}", start + 1)
    return text[start : following if following > start else len(text)]


class TheLauncherAcceptsWhatTerraformGivesIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = entries(container("launcher"))
        cls.full = {**cls.env, launcher.KEY_ENV: "sk-ant-test-value"}

    def test_the_names_are_exactly_the_contracts_launcher_group_and_the_scrape_switch(self):
        self.assertEqual(set(self.env), {*pc.ENV_LAUNCHER.values(), "AZMON_COLLECT_ENV"})

    def test_the_configuration_is_accepted_and_every_value_is_the_intended_one(self):
        config = launcher.Config.from_environment(self.full)
        self.assertEqual(config.image, IMAGE)
        self.assertEqual(config.namespace, pc.WORKER_NAMESPACE)
        self.assertEqual(config.max_pods, int(CEILING))
        self.assertEqual(config.repo_cap, pc.PER_REPO_CAP)
        self.assertEqual(config.deadline_clamp, pc.DEADLINE_CLAMP_SECONDS)
        self.assertEqual(config.allowed_uid, pc.SUPERVISOR_UID)
        self.assertEqual(config.classes, (pc.DEFAULT_CLASS,))
        self.assertEqual(config.socket_path, pc.LAUNCHER_SOCKET)
        self.assertEqual(config.relay_url, f"http://factory-relay-svc.factory.svc.cluster.local:{pc.RELAY_PORT}")
        self.assertEqual(config.manager_address, "factory.factory.svc.cluster.local")
        self.assertEqual(config.api_public_host, FQDN)

    def test_dropping_any_required_entry_makes_the_launcher_refuse_to_start(self):
        required = [pc.ENV_LAUNCHER[key] for key in ("image", "max_pods", "relay_url", "api_public_host", "manager_address")]
        for name in required:
            with self.subTest(name=name):
                without = {key: value for key, value in self.full.items() if key != name}
                with self.assertRaises(launcher.ConfigRefused):
                    launcher.Config.from_environment(without)

    def test_the_key_is_the_one_secret_and_the_launcher_refuses_without_it(self):
        with self.assertRaises(launcher.ConfigRefused):
            launcher.Config.from_environment(self.env)
        self.assertEqual([name for name in self.env if "KEY" in name or "TOKEN" in name or "SECRET" in name], [])

    def test_only_the_default_class_is_enabled_and_it_is_one_the_contract_has(self):
        self.assertEqual(self.env[pc.ENV_LAUNCHER["classes"]], pc.DEFAULT_CLASS)
        self.assertIn(pc.DEFAULT_CLASS, pc.CLASSES)

    def test_the_manager_address_is_not_the_relay_name_so_the_canary_forbids_one_and_the_policy_allows_the_other(self):
        self.assertNotEqual(self.env[pc.ENV_LAUNCHER["manager_address"]], self.env[pc.ENV_LAUNCHER["relay_url"]].split("//")[1].split(":")[0])


class TheRelayAcceptsWhatTerraformGivesIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = entries(container("relay"))

    def test_the_names_are_the_contracts_relay_group_and_the_scrape_switch(self):
        self.assertEqual(set(self.env), {*pc.ENV_RELAY.values(), "AZMON_COLLECT_ENV"})

    def test_the_relay_finds_no_secret_in_it_and_reads_the_port_and_the_spool(self):
        self.assertIsNone(relay.check_environment(self.env, token_path="/nonexistent/token"))
        self.assertEqual(relay._port(self.env), pc.RELAY_PORT)
        self.assertEqual(self.env[pc.ENV_RELAY["spool_dir"]], pc.SPOOL_DIR)

    def test_the_value_kubernetes_injects_for_a_service_called_factory_relay_is_what_the_relay_refuses(self):
        injected = {**self.env, pc.ENV_RELAY["port"]: "tcp://10.0.0.7:8090"}
        self.assertIsNone(relay._port(injected))
        # ... which is why the environment sets the name itself and the Service is named something else.
        self.assertEqual(self.env[pc.ENV_RELAY["port"]], str(pc.RELAY_PORT))
        self.assertNotIn("factory-relay.", " ".join(self.env.values()))

    def test_a_credential_a_cloud_identity_or_a_token_file_in_the_pod_would_make_it_refuse(self):
        for name in ("ANTHROPIC_API_KEY", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE", "GITHUB_APP_PRIVATE_KEY"):
            with self.subTest(name=name):
                self.assertEqual(relay.check_environment({**self.env, name: "x"}, token_path="/nonexistent/token"), "secret_in_environment")
        self.assertEqual(relay.check_environment(self.env, token_path=str(Path(__file__))), "kubernetes_token_present")


class TheMainContainerAndTheClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = entries(local_block("pod_backend_main_env_all"))

    def test_the_names_are_the_contracts_manager_group(self):
        self.assertEqual(set(self.env), set(pc.ENV_MANAGER.values()))

    def test_the_switch_reads_on_and_the_client_reads_the_socket_and_the_ceiling_is_the_pool_ceiling(self):
        self.assertTrue(podexecution.switch_is_on(self.env))
        self.assertEqual(podexecution.switch_value(self.env), pc.POD_BACKEND_ON)
        self.assertEqual(launcher_client.LauncherClient.from_environment(self.env)._path, pc.LAUNCHER_SOCKET)
        self.assertEqual(self.env[pc.ENV_MANAGER["max_pod_sessions"]], CEILING)
        self.assertEqual(self.env[pc.ENV_MANAGER["spool_dir"]], pc.SPOOL_DIR)

    def test_the_launcher_and_the_manager_have_the_same_ceiling_and_the_same_socket(self):
        launcher_env = entries(container("launcher"))
        self.assertEqual(launcher_env[pc.ENV_LAUNCHER["max_pods"]], self.env[pc.ENV_MANAGER["max_pod_sessions"]])
        self.assertEqual(launcher_env[pc.ENV_LAUNCHER["socket"]], self.env[pc.ENV_MANAGER["launcher_socket"]])


class TheInitContainerMakesWhatTheProgramsDemand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = local_block("pod_backend_init_containers_all")
        lines = [evaluate(f'"{line}"') for line in re.findall(r'"((?:chown|chmod) [^"]+)"', text)]
        cls.owners = {}
        cls.modes = {}
        for line in lines:
            owner = re.fullmatch(r"chown (\d+):(\d+) (\S+)", line)
            mode = re.fullmatch(r"chmod (\d+) (\S+)", line)
            if owner:
                cls.owners[owner.group(3)] = (int(owner.group(1)), int(owner.group(2)))
            elif mode:
                cls.modes[mode.group(2)] = int(mode.group(1), 8)
        cls.lines = lines

    def directory(self, path):
        uid, gid = self.owners[path]
        return os.stat_result((stat.S_IFDIR | self.modes[path], 0, 0, 2, uid, gid, 4096, 0, 0, 0))

    def test_the_command_covers_exactly_the_two_directories_of_the_contract(self):
        self.assertEqual(set(self.owners), {pc.LAUNCHER_SOCKET_DIR, pc.SPOOL_DIR})
        self.assertEqual(set(self.modes), set(self.owners))
        self.assertEqual(len(self.lines), 4)

    def test_the_socket_directory_is_what_the_launcher_and_its_client_require(self):
        info = self.directory(pc.LAUNCHER_SOCKET_DIR)
        launcher.check_directory(info, pc.LAUNCHER_UID)  # no exception: owned by the launcher, not writable by group or others
        self.assertEqual(info.st_gid, pc.SHARED_GID)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o750)
        # The supervisor (uid 10001, group 10001) may enter it; the worker (10002, group 10010) may not.
        self.assertTrue(info.st_mode & stat.S_IXGRP)
        self.assertFalse(info.st_mode & stat.S_IRWXO)
        self.assertNotEqual(pc.WORKER_GID, pc.SHARED_GID)

    def test_the_spool_root_is_what_the_relay_requires(self):
        info = self.directory(pc.SPOOL_DIR)
        relay.check_root(info, pc.RELAY_UID, pc.SHARED_GID)  # no exception
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o2770)

    def test_a_wrong_owner_or_mode_would_be_refused_by_the_programs(self):
        info = self.directory(pc.SPOOL_DIR)
        wrong_owner = os.stat_result((info.st_mode, 0, 0, 2, pc.LAUNCHER_UID, info.st_gid, 4096, 0, 0, 0))
        with self.assertRaises(relay.SpoolRefused):
            relay.check_root(wrong_owner, pc.RELAY_UID, pc.SHARED_GID)
        without_setgid = os.stat_result((stat.S_IFDIR | 0o770, 0, 0, 2, info.st_uid, info.st_gid, 4096, 0, 0, 0))
        with self.assertRaises(relay.SpoolRefused):
            relay.check_root(without_setgid, pc.RELAY_UID, pc.SHARED_GID)
        group_writable = os.stat_result((stat.S_IFDIR | 0o770, 0, 0, 2, pc.LAUNCHER_UID, pc.SHARED_GID, 4096, 0, 0, 0))
        with self.assertRaises(launcher.SocketRefused):
            launcher.check_directory(group_writable, pc.LAUNCHER_UID)

    def test_the_uids_of_the_sidecars_are_the_ones_the_directories_are_given_to(self):
        text = tf_text("pod_backend.tf")
        self.assertIn("runAsUser  = local.worker_contract.ids.launcher_uid", text)
        self.assertIn("runAsUser  = local.worker_contract.ids.relay_uid", text)
        self.assertEqual(self.owners[pc.LAUNCHER_SOCKET_DIR][0], pc.LAUNCHER_UID)
        self.assertEqual(self.owners[pc.SPOOL_DIR][0], pc.RELAY_UID)


if __name__ == "__main__":
    unittest.main()
