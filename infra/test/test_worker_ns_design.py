"""Design guards for the worker namespace of the pod backend (slice 15a, issues #144 and #145).

They read the .tf files as text (standard library only, like check_refs.py), so they run in CI as
`python3 -I -m unittest discover -s infra/test` with no package installed. A text test can check defaults, gating, the rules
of a policy and a wording; it cannot prove that a plan shows no line, that Cilium enforces `ipBlock.except` or that the
agent honours `exclude_namespaces` - only a real `terraform plan` and a live run can, and the pull request body says so.

What is pinned here:
  * ONE variable, `pod_backend_enabled`, default false, gates the namespace, the service account and the NetworkPolicy
    (count 0 by default), and nothing sets it in a committed file;
  * the namespace is privileged, carries the three preconditions, both role dependencies and depends on the Container Insights
    ConfigMap; the service account mounts no token and no RoleBinding names it;
  * the NetworkPolicy denies ingress and allows egress to exactly cluster DNS, the relay port of the manager pod and TCP 443
    outside five ranges, and each egress rule has exactly ONE `to` block (the selectors of one block are ANDed, two blocks are
    ORed: a split would turn "the manager pod" into "every pod of the namespace" or "any pod with the label");
  * the log exclusion is the full list under stdout AND stderr, only when the variable is on, and the data of the off state
    is byte-identical to the text that main produced before the exclusion existed;
  * the prepared policy of monitoring.tf carries the relay rule if and only if the variable is on, and each of its peers
    selects the namespace and the pod in ONE entry of `from` (the same AND);
  * the stale "would be decoration" wording of infra/k8s.tf is replaced;
  * the ORDER of slice 16: any Role, RoleBinding, ClusterRole or ClusterRoleBinding in infra/ that names the worker namespace
    or the worker service account lists the default-deny policy in its `depends_on` (the two of worker_rbac.tf do; the scan's
    own tests run it on synthetic examples), and the comment, the README and docs/pod-backend.md say so.

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_worker_ns_design -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

# Sibling modules are imported by bare name: put this directory on sys.path so the import also works when the
# module is loaded by dotted name from the repository root (python3 -m unittest infra.test.<module>).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_refs

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent

ROLES = ("azurerm_role_assignment.aks_cluster_admin", "azurerm_role_assignment.aks_cluster_user")
WORKER_OBJECTS = {
    "kubernetes_namespace_v1": "worker",
    "kubernetes_service_account_v1": "worker",
    "kubernetes_network_policy_v1": "worker_default_deny",
}
# The ConfigMap text that main produced before the exclusion existed (infra/locals.tf, k8s_insights_agent_config): the off
# state must still render exactly this, byte for byte.
OFF_TEXT = "\n".join(
    [
        "[log_collection_settings]",
        "  [log_collection_settings.stdout]",
        "    enabled = true",
        "  [log_collection_settings.stderr]",
        "    enabled = true",
        "  [log_collection_settings.env_var]",
        "    enabled = false",
    ]
)
EXCLUDED = ["kube-system", "gatekeeper-system", "factory-workers"]


def uncommented(text):
    """The text without comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def code(name):
    """One .tf file of infra/ without comment lines."""
    return uncommented((INFRA / name).read_text(encoding="utf-8"))


def block(text, header):
    """The top-level (or nested) block that starts with `header`, braces included."""
    start = text.index(header)
    view = check_refs.blank_view(text)
    return text[start : check_refs.match_brace(view, text.index("{", start))]


def resource(file, kind, name):
    return block(code(file), f'resource "{kind}" "{name}"')


def variable(name):
    return block(code("variables.tf"), f'variable "{name}"')


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


NESTED_HEADER = re.compile(r"(?<![\w.\"])([A-Za-z_]\w*)[ \t]*\{")


def children(parent, name):
    """The DIRECT child blocks `name { ... }` of a block (its text from the header to the closing brace), braces included.

    A block nested two levels down is not a child, and neither is an attribute such as `match_labels = { ... }`.
    """
    view = check_refs.blank_view(parent)
    inner_start = view.index("{") + 1
    inner_end = check_refs.match_brace(view, inner_start - 1) - 1
    found = []
    position = inner_start
    while True:
        header = NESTED_HEADER.search(view, position, inner_end)
        if header is None:
            return found
        end = check_refs.match_brace(view, header.end() - 1)
        if header.group(1) == name:
            found.append(parent[header.start() : end])
        position = end


def closing_bracket(view, open_index):
    """Index of the `]` that closes the `[` at `open_index` (in a blank view)."""
    depth = 0
    for position, char in enumerate(view[open_index:], start=open_index):
        depth += {"[": 1, "]": -1}.get(char, 0)
        if depth == 0:
            return position
    raise AssertionError("a list is never closed")


def list_bodies(text, opening):
    """The text inside every `<opening>[ ... ]` list of `text` (strings and comments do not count), outermost lists only."""
    view = check_refs.blank_view(text)
    return [text[start.end() : closing_bracket(view, start.end() - 1)] for start in re.finditer(opening, view)]


def depends_on(text):
    found = re.search(r"depends_on\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if found is None:
        raise AssertionError("the block has no depends_on")
    return found.group(1)


def locals_assignment(file, name):
    """The right-hand side text of `  <name> = ...` in a locals block, up to the next attribute at the same indent."""
    text = code(file)
    found = re.search(rf"(?m)^  {re.escape(name)}\s*=\s*", text)
    if found is None:
        raise AssertionError(f"no locals attribute {name} in {file}")
    rest = text[found.end() :].split("\n}\n", 1)[0]
    following = re.search(r"(?m)^  (?:\w+)\s*=", rest)
    return rest[: following.start()] if following else rest


POLICY_ADDRESS = "kubernetes_network_policy_v1.worker_default_deny"
ROLE_LIKE_TYPE = re.compile(r"^kubernetes_(?:cluster_)?role(?:_binding)?(?:_v1)?$")
ROLE_LIKE_KIND = re.compile(r'\bkind\s*=\s*"(?:Cluster)?Role(?:Binding)?"')
# Every spelling by which a resource names the worker namespace or the worker service account (`factory-worker` is also the
# first part of the namespace name `factory-workers`; `var.pod_backend_enabled` and its count local gate the objects of the fence).
WORKER_SPELLINGS = (
    "kubernetes_namespace_v1.worker",
    "kubernetes_service_account_v1.worker",
    "worker_contract.names.worker_namespace",
    "worker_contract.names.worker_service_account",
    "local.worker_ns_count",
    "var.pod_backend_enabled",
    "factory-worker",
)


def locals_of(texts):
    """{name: expression text} of every attribute of every top-level `locals` block of `texts` ({file name: text})."""
    found = {}
    for text in texts.values():
        for inner in re.findall(r"(?ms)^locals \{\n(.*?)^\}$", uncommented(text)):
            heads = list(re.finditer(r"(?m)^  (\w+)\s*=", inner))
            for head, following in zip(heads, [*heads[1:], None], strict=True):
                found[head.group(1)] = inner[head.end() : following.start() if following else len(inner)]
    return found


def names_worker(text, aliases, seen=()):
    """Whether `text` names the worker namespace or account: by a spelling, or through a `local.` that does (at any depth)."""
    if any(spelling in text for spelling in WORKER_SPELLINGS):
        return True
    # `worker_contract` holds the names next to many others: only its exact paths count (the spellings above).
    names = [name for name in re.findall(r"\blocal\.(\w+)", text) if name in aliases and name not in seen and name != "worker_contract"]
    return any(names_worker(aliases[name], aliases, (*seen, name)) for name in names)


def role_like_resources(texts):
    """[(file, address, body)] of every Role, RoleBinding, ClusterRole or ClusterRoleBinding of `texts` ({file name: text}).

    A typed resource (`kubernetes_role_v1`, `kubernetes_role_binding_v1`, ...) and a `kubernetes_manifest` whose `kind` is one of
    the four both count.
    """
    found = []
    for name, text in sorted(texts.items()):
        plain = uncommented(text)
        for kind, label in re.findall(r'(?m)^resource "(kubernetes_[a-z0-9_]+)" "([^"]+)"', plain):
            body = block(plain, f'resource "{kind}" "{label}"')
            if ROLE_LIKE_TYPE.match(kind) or ROLE_LIKE_KIND.search(body):
                found.append((name, f"{kind}.{label}", body))
    return found


def unordered_role_like_resources(texts):
    """The Role-like resources of `texts` that name the worker namespace or account and do not list the policy in `depends_on`."""
    aliases = locals_of(texts)
    found = []
    for name, address, body in role_like_resources(texts):
        listed = re.search(r"depends_on\s*=\s*\[(.*?)\]", body, re.DOTALL)
        ordered = listed is not None and POLICY_ADDRESS in listed.group(1)
        if names_worker(body, aliases) and not ordered:
            found.append(f"{name}: {address}")
    return found


def render_log_settings(on):
    """The log-data-collection-settings text that infra/locals.tf produces, rendered from its own text (no HCL evaluator needed)."""
    data = locals_assignment("locals.tf", "k8s_insights_agent_config")
    region = data[data.index('"log-data-collection-settings"') :]
    region = region[region.index("concat(") :]
    lines = []
    for found in re.finditer(r'"((?:[^"\\]|\\.)*)"|local\.k8s_log_exclusion_lines', region):
        if found.group(0) == "local.k8s_log_exclusion_lines":
            if on:
                lines.append(f"    exclude_namespaces = {json.dumps(EXCLUDED, separators=(',', ':'))}")
        else:
            lines.append(found.group(1))
    return "\n".join(lines)


class OneVariableGatesEverything(unittest.TestCase):
    def test_the_variable_is_a_bool_that_is_off_by_default(self):
        text = variable("pod_backend_enabled")
        self.assertRegex(text, r"(?m)^\s*type\s*=\s*bool\s*$")
        self.assertRegex(text, r"(?m)^\s*default\s*=\s*false\s*$")

    def test_the_description_says_what_turning_it_off_does_and_what_it_needs(self):
        text = squash(variable("pod_backend_enabled"))
        for needle in ("network_data_plane = cilium", "nap_mode = Auto", "worker_nodepool_enabled", "DELETES the namespace", "drain gate", "byte-identical"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_count_local_is_the_variable_and_every_object_uses_it(self):
        self.assertEqual(squash(locals_assignment("worker_ns.tf", "worker_ns_count")), "var.pod_backend_enabled ? 1 : 0")
        for kind, name in WORKER_OBJECTS.items():
            with self.subTest(resource=f"{kind}.{name}"):
                self.assertRegex(resource("worker_ns.tf", kind, name), r"(?m)^\s*count\s*=\s*local\.worker_ns_count\s*$")

    def test_the_file_declares_exactly_these_three_resources_and_nothing_else_is_gated_by_a_second_variable(self):
        found = re.findall(r'(?m)^resource "([a-z0-9_]+)" "([A-Za-z0-9_-]+)"', code("worker_ns.tf"))
        self.assertEqual(sorted(found), sorted(WORKER_OBJECTS.items()))
        text = code("worker_ns.tf")
        self.assertEqual(sorted(set(re.findall(r"\bvar\.(\w+)", text))), sorted({"pod_backend_enabled", "aks_pod_cidr", "aks_service_cidr", "node_subnet_prefix", "network_data_plane", "nap_mode", "worker_nodepool_enabled"}))

    def test_the_variable_is_read_by_the_three_places_that_change_with_it(self):
        for name in ("worker_ns.tf", "locals.tf", "monitoring.tf"):
            with self.subTest(file=name):
                self.assertIn("var.pod_backend_enabled", code(name))

    def test_no_committed_tfvars_file_sets_it_and_the_example_only_comments_it(self):
        for path in sorted(INFRA.glob("*.auto.tfvars")):
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?m)^\s*pod_backend_enabled\s*=")
        example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
        self.assertNotRegex(example, r"(?m)^\s*pod_backend_enabled\s*=")
        self.assertRegex(example, r"(?m)^#\s*pod_backend_enabled\s*=")

    def test_the_readme_names_the_variable_and_the_new_objects(self):
        readme = (INFRA / "README.md").read_text(encoding="utf-8")
        for needle in ("`pod_backend_enabled`", "kubernetes_namespace_v1.worker", "kubernetes_service_account_v1.worker", "kubernetes_network_policy_v1.worker_default_deny"):
            with self.subTest(needle=needle):
                self.assertIn(needle, readme)


class Namespace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = resource("worker_ns.tf", "kubernetes_namespace_v1", "worker")

    def test_it_is_privileged_and_named_and_labelled_from_the_contract(self):
        text = squash(self.namespace)
        self.assertIn("name = local.worker_contract.names.worker_namespace", text)
        self.assertIn('"pod-security.kubernetes.io/enforce" = "privileged"', text)
        self.assertIn('"${local.worker_contract.labels.prefix}role" = local.worker_contract.labels.role_value', text)
        self.assertIn("local.k8s_labels", text)
        for weaker in ("baseline", "restricted"):
            self.assertNotIn(f'"{weaker}"', text)

    def test_it_plans_only_with_cilium_nap_and_the_worker_pool(self):
        preconditions = re.findall(r"condition\s*=\s*([^\n]+)", self.namespace)
        self.assertEqual(preconditions, ['var.network_data_plane == "cilium"', 'var.nap_mode == "Auto"', "var.worker_nodepool_enabled"])
        self.assertEqual(self.namespace.count("precondition {"), 3)

    def test_it_waits_for_both_role_assignments_and_for_the_log_exclusion(self):
        # R1-3: the agent ConfigMap holds the exclusion, so it exists before any worker pod can.
        for role in ROLES:
            self.assertIn(role, depends_on(self.namespace))
        self.assertIn("kubernetes_config_map_v1.insights_agent", depends_on(self.namespace))

    def test_the_agent_configmap_itself_waits_for_the_roles_so_the_chain_reaches_them(self):
        configmap = resource("k8s.tf", "kubernetes_config_map_v1", "insights_agent")
        for role in ROLES:
            self.assertIn(role, depends_on(configmap))
        self.assertIn("data = local.k8s_insights_agent_config.data", configmap)


class ServiceAccount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.account = resource("worker_ns.tf", "kubernetes_service_account_v1", "worker")

    def test_it_mounts_no_token_and_carries_no_identity_or_secret(self):
        self.assertRegex(self.account, r"(?m)^\s*automount_service_account_token\s*=\s*false\s*$")
        self.assertIn("name      = local.worker_contract.names.worker_service_account", self.account)
        self.assertIn("kubernetes_namespace_v1.worker[0].metadata[0].name", self.account)
        for forbidden in ("azure.workload.identity", "secret {", "image_pull_secret", "annotations"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.account)
        for role in ROLES:
            self.assertIn(role, depends_on(self.account))

    def test_no_binding_of_any_role_is_declared_in_this_file_and_none_anywhere_names_the_account(self):
        text = code("worker_ns.tf")
        for forbidden in ("role_binding", "RoleBinding", "kubernetes_role", "ClusterRole"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        # Slice 16 binds a Role to the service account `factory` of the manager namespace, never to this one: a binding that
        # names the worker account (by resource or by contract name) would give a worker pod a Kubernetes credential's rights.
        bindings = 0
        for path in sorted(INFRA.glob("*.tf")):
            for found in re.finditer(r'(?m)^resource "(kubernetes_[a-z0-9_]+)" "([^"]+)"', code(path.name)):
                body = resource(path.name, found.group(1), found.group(2))
                if "role_binding" not in found.group(1) and not re.search(r'kind\s*=\s*"(?:Cluster)?RoleBinding"', body):
                    continue
                bindings += 1
                with self.subTest(file=path.name, binding=found.group(2)):
                    self.assertNotIn("worker_service_account", body)
                    self.assertNotIn("kubernetes_service_account_v1.worker", body)
                    self.assertNotIn("factory-worker", body)
        self.assertGreaterEqual(bindings, 1, "the scan found no binding at all (the Prometheus ClusterRoleBinding): the pattern no longer matches")


class DefaultDenyPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = resource("worker_ns.tf", "kubernetes_network_policy_v1", "worker_default_deny")
        cls.spec = block(cls.policy, "spec")
        view = check_refs.blank_view(cls.spec)
        cls.egress = [cls.spec[found.start() : check_refs.match_brace(view, found.end() - 1)] for found in re.finditer(r"(?m)^    egress \{", cls.spec)]

    def test_it_selects_every_pod_of_the_namespace_and_covers_both_directions(self):
        self.assertIn("namespace = kubernetes_namespace_v1.worker[0].metadata[0].name", self.policy)
        self.assertRegex(self.spec, r"(?m)^    pod_selector \{\}$")
        self.assertRegex(self.spec, r'(?m)^    policy_types = \["Ingress", "Egress"\]$')

    def test_ingress_is_denied_entirely_there_is_no_ingress_block(self):
        # An omitted `ingress` list admits nothing; an empty `ingress {}` block would admit everything.
        self.assertNotRegex(self.spec, r"(?m)^\s*ingress\s*(\{|=)")

    def test_it_has_exactly_three_egress_rules_in_the_documented_order(self):
        self.assertEqual(len(self.egress), 3)

    def test_the_first_rule_is_cluster_dns_only(self):
        dns = squash(self.egress[0])
        self.assertEqual(re.findall(r'port\s*=\s*"(\d+)" protocol\s*=\s*"(\w+)"', dns), [("53", "UDP"), ("53", "TCP")])
        self.assertIn('"kubernetes.io/metadata.name" = "kube-system"', dns)
        self.assertIn('"k8s-app" = "kube-dns"', dns)
        self.assertNotIn("ip_block", dns)

    def test_the_second_rule_is_the_relay_port_of_the_manager_pod_only(self):
        relay = squash(self.egress[1])
        self.assertEqual(re.findall(r"port\s*=\s*([^\s]+) protocol\s*=\s*\"(\w+)\"", relay), [("tostring(local.worker_contract.ports.relay)", "TCP")])
        self.assertIn('"kubernetes.io/metadata.name" = local.k8s_namespace', relay)
        self.assertIn("match_labels = local.k8s_selector_labels", relay)
        self.assertNotIn("ip_block", relay)

    def test_the_third_rule_is_tcp_443_outside_five_ranges(self):
        internet = squash(self.egress[2])
        self.assertEqual(re.findall(r'port\s*=\s*"(\d+)" protocol\s*=\s*"(\w+)"', internet), [("443", "TCP")])
        self.assertIn('cidr = "0.0.0.0/0"', internet)
        self.assertIn("except = local.worker_egress_excluded_cidrs", internet)
        self.assertNotIn("namespace_selector", internet)

    def test_the_excluded_ranges_are_the_metadata_service_the_host_service_and_the_three_cluster_ranges(self):
        found = locals_assignment("worker_ns.tf", "worker_egress_excluded_cidrs")
        self.assertEqual(
            re.findall(r'"([^"]+)"|\b(var\.\w+)', found),
            [("169.254.0.0/16", ""), ("168.63.129.16/32", ""), ("", "var.aks_pod_cidr"), ("", "var.aks_service_cidr"), ("", "var.node_subnet_prefix")],
        )

    def test_no_other_port_or_destination_is_allowed(self):
        ports = re.findall(r'port\s*=\s*("?[^\s"]+"?)', self.spec)
        self.assertEqual(sorted(ports), sorted(['"53"', '"53"', "tostring(local.worker_contract.ports.relay)", '"443"']))
        self.assertEqual(re.findall(r"0\.0\.0\.0/\d+", self.spec), ["0.0.0.0/0"])
        self.assertEqual(self.spec.count("ip_block"), 1)

    def test_it_waits_for_both_role_assignments(self):
        for role in ROLES:
            self.assertIn(role, depends_on(self.policy))


class EgressRuleShape(unittest.TestCase):
    """AND versus OR. In a NetworkPolicy the selectors of ONE `to` entry are ANDed ("the manager pod, in the factory namespace"),
    while two `to` entries of one rule are ORed ("every pod of that namespace, OR every pod with that label in any namespace").
    Splitting the namespace selector and the pod selector of the DNS rule or of the relay rule is a valid policy that silently
    widens the fence, and the substring tests above still pass on it: so the shape itself is pinned."""

    @classmethod
    def setUpClass(cls):
        policy = resource("worker_ns.tf", "kubernetes_network_policy_v1", "worker_default_deny")
        cls.spec = block(policy, "spec")
        cls.rules = children(cls.spec, "egress")

    def test_every_egress_rule_has_exactly_one_to_block(self):
        self.assertEqual([len(children(rule, "to")) for rule in self.rules], [1, 1, 1])
        self.assertEqual(len(re.findall(r"(?m)^\s*to\s*\{", self.spec)), 3)

    def test_the_dns_and_the_relay_rule_select_the_namespace_and_the_pod_in_that_one_block(self):
        for number, namespace, pod in (
            (0, '"kubernetes.io/metadata.name" = "kube-system"', '"k8s-app" = "kube-dns"'),
            (1, '"kubernetes.io/metadata.name" = local.k8s_namespace', "match_labels = local.k8s_selector_labels"),
        ):
            with self.subTest(rule=number):
                (peer,) = children(self.rules[number], "to")
                (by_namespace,) = children(peer, "namespace_selector")
                (by_pod,) = children(peer, "pod_selector")
                self.assertIn(namespace, squash(by_namespace))
                self.assertIn(pod, squash(by_pod))
                # No selector sits anywhere else in the rule, and the peer holds nothing else.
                self.assertEqual(self.rules[number].count("namespace_selector"), 1)
                self.assertEqual(self.rules[number].count("pod_selector"), 1)
                self.assertNotIn("ip_block", peer)

    def test_the_internet_rule_is_one_to_block_with_one_ip_block_and_no_selector(self):
        (peer,) = children(self.rules[2], "to")
        self.assertEqual(len(children(peer, "ip_block")), 1)
        self.assertEqual(self.rules[2].count("ip_block"), 1)
        for selector in ("namespace_selector", "pod_selector"):
            self.assertNotIn(selector, self.rules[2])

    def test_the_ports_are_beside_the_to_block_never_inside_it(self):
        for number, rule in enumerate(self.rules):
            with self.subTest(rule=number):
                (peer,) = children(rule, "to")
                self.assertNotIn("ports", peer)
                self.assertGreaterEqual(len(children(rule, "ports")), 1)


class TheShapeScannerSeesASplit(unittest.TestCase):
    """The scanner of the class above is real code: on a split rule it counts two blocks (a test of the helper, not of the Terraform)."""

    JOINED = "egress {\n  to {\n    namespace_selector {}\n    pod_selector {}\n  }\n}\n"
    SPLIT = "egress {\n  to {\n    namespace_selector {}\n  }\n  to {\n    pod_selector {}\n  }\n}\n"

    def test_joined_is_one_block_with_both_selectors_and_split_is_two_blocks(self):
        (peer,) = children(self.JOINED, "to")
        self.assertEqual((len(children(peer, "namespace_selector")), len(children(peer, "pod_selector"))), (1, 1))
        self.assertEqual(len(children(self.SPLIT, "to")), 2)

    def test_a_nested_block_and_a_map_attribute_are_not_children(self):
        text = "egress {\n  to {\n    pod_selector {\n      to {}\n    }\n  }\n  labels = {\n    to = 1\n  }\n}\n"
        self.assertEqual(len(children(text, "to")), 1)
        self.assertEqual(children(text, "labels"), [])


class RoleLikeResourcesAreOrderedAfterThePolicy(unittest.TestCase):
    """Slice 16 adds a Role and a RoleBinding in this namespace. The default-deny policy must exist BEFORE any of them does,
    so that no moment exists in which an identity may create a pod without the fence: Terraform orders two resources that
    share only the namespace as it likes (it may create them at the same time), so the dependency must be written down. Since slice 16
    the real tree holds the Role and the RoleBinding of the launcher (worker_rbac.tf), and the scan of it passes because both list the
    policy; its synthetic examples below show that it fails when the dependency is missing."""

    TEMPLATE = 'resource "{kind}" "launcher" {{\n{count}  metadata {{\n    namespace = {namespace}\n  }}\n{depends}}}\n'
    NAMESPACE = "kubernetes_namespace_v1.worker[0].metadata[0].name"

    def scan(self, kind="kubernetes_role_binding_v1", namespace=NAMESPACE, depends="", count="  count = local.worker_ns_count\n", definitions=""):
        text = self.TEMPLATE.format(kind=kind, namespace=namespace, depends=depends, count=count)
        return unordered_role_like_resources({"slice16.tf": text, "locals.tf": definitions})

    def test_the_real_tree_orders_its_role_like_resources_of_the_worker_namespace_and_the_scan_still_sees_the_others(self):
        texts = {path.name: path.read_text(encoding="utf-8") for path in sorted(INFRA.glob("*.tf"))}
        seen = [address for _, address, _ in role_like_resources(texts)]
        self.assertGreaterEqual(len(seen), 2, "the scan found no Prometheus ClusterRole or binding: the pattern no longer matches")
        self.assertLessEqual({"kubernetes_role_v1.worker_launcher", "kubernetes_role_binding_v1.worker_launcher"}, set(seen))
        self.assertEqual(unordered_role_like_resources(texts), [])

    def test_a_binding_or_role_that_names_the_namespace_or_the_account_without_the_dependency_is_caught(self):
        # `count=""`: no count local, so only the spelling of the namespace or the account makes the resource a worker resource.
        for kind in ("kubernetes_role_binding_v1", "kubernetes_role_v1", "kubernetes_cluster_role_binding_v1", "kubernetes_cluster_role_v1"):
            for namespace in (
                self.NAMESPACE,
                "local.worker_contract.names.worker_namespace",
                '"factory-workers"',
                "kubernetes_service_account_v1.worker[0].metadata[0].namespace",
                "local.worker_contract.names.worker_service_account",
            ):
                with self.subTest(kind=kind, namespace=namespace):
                    self.assertEqual(self.scan(kind, namespace, count=""), [f"slice16.tf: {kind}.launcher"])

    def test_a_namespace_that_a_local_holds_is_caught_and_an_unrelated_local_is_not(self):
        for definitions in (
            "locals {\n  ns = local.worker_contract.names.worker_namespace\n}\n",
            'locals {\n  ns = local.inner\n  inner = "factory-workers"\n}\n',
            "locals {\n  ns = kubernetes_namespace_v1.worker[0].metadata[0].name\n}\n",
        ):
            with self.subTest(definitions=definitions):
                self.assertEqual(self.scan(namespace="local.ns", count="", definitions=definitions), ["slice16.tf: kubernetes_role_binding_v1.launcher"])
                self.assertEqual(self.scan(namespace="local.ns", count="", depends="  depends_on = [kubernetes_network_policy_v1.worker_default_deny]\n", definitions=definitions), [])
        self.assertEqual(self.scan(namespace="local.ns", count="", definitions='locals {\n  ns = "factory"\n}\n'), [])

    def test_a_manifest_whose_kind_is_a_role_is_caught_too(self):
        for kind in ("Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"):
            with self.subTest(kind=kind):
                text = f'resource "kubernetes_manifest" "x" {{\n  manifest = {{\n    kind = "{kind}"\n    metadata = {{ namespace = "factory-workers" }}\n  }}\n}}\n'
                self.assertEqual(unordered_role_like_resources({"x.tf": text}), ["x.tf: kubernetes_manifest.x"])

    def test_the_dependency_must_be_the_policy_in_depends_on_not_the_namespace_or_a_mention(self):
        for depends in (
            "  depends_on = [kubernetes_namespace_v1.worker]\n",
            "  depends_on = [\n    kubernetes_namespace_v1.worker,\n    kubernetes_service_account_v1.worker,\n  ]\n",
            "  # depends_on = [kubernetes_network_policy_v1.worker_default_deny]\n",
            "  note = kubernetes_network_policy_v1.worker_default_deny[0].id\n",
        ):
            with self.subTest(depends=depends):
                self.assertEqual(len(self.scan(depends=depends)), 1)

    def test_with_the_policy_in_depends_on_it_passes(self):
        for depends in (
            "  depends_on = [kubernetes_network_policy_v1.worker_default_deny]\n",
            "  depends_on = [\n    kubernetes_namespace_v1.worker,\n    kubernetes_network_policy_v1.worker_default_deny,\n  ]\n",
        ):
            with self.subTest(depends=depends):
                self.assertEqual(self.scan(depends=depends), [])

    def test_a_role_like_resource_of_another_namespace_needs_no_dependency(self):
        self.assertEqual(self.scan(namespace="local.k8s_namespace", count=""), [])
        self.assertEqual(self.scan("kubernetes_cluster_role_v1", "local.k8s_monitoring_namespace", count=""), [])

    def test_the_comment_the_readme_and_the_design_record_say_it(self):
        raw = (INFRA / "worker_ns.tf").read_text(encoding="utf-8")
        before = raw.split('resource "kubernetes_network_policy_v1" "worker_default_deny"', 1)[0]
        self.assertIn("# ORDER", before)
        comment = before.rsplit("# ORDER", 1)[1]
        lines = [line for line in comment.splitlines()[1:] if line.strip()]
        self.assertTrue(all(line.startswith("#") for line in lines), "the comment is not next to the policy")
        flat = squash(comment.replace("# ", ""))
        for needle in ("must exist BEFORE any Role or RoleBinding", "MUST list kubernetes_network_policy_v1.worker_default_deny in its `depends_on`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)
        sentence = "MUST `depends_on` `kubernetes_network_policy_v1.worker_default_deny`"
        self.assertIn(sentence, squash((INFRA / "README.md").read_text(encoding="utf-8")))
        record = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        section = record.split("## Terraform B1", 1)[1].split("\n## ", 1)[0]
        self.assertIn(sentence, squash(section))
        (row,) = [line for line in record.splitlines() if line.startswith("| 16, 17 |")]
        self.assertIn("`depends_on` `kubernetes_network_policy_v1.worker_default_deny`", row)


class LogExclusion(unittest.TestCase):
    def test_the_full_default_list_plus_the_worker_namespace_is_spelled_out(self):
        listed = squash(locals_assignment("locals.tf", "k8s_log_excluded_namespaces"))
        self.assertEqual(listed, '["kube-system", "gatekeeper-system", local.worker_contract.names.worker_namespace]')
        # EXCLUDED is what the renderer below substitutes: tie its last entry to the namespace name the real contract local holds.
        contract = locals_assignment("locals.tf", "worker_contract")
        self.assertEqual(re.findall(r'(?m)^\s*worker_namespace\s*=\s*"([^"]+)"', contract), [EXCLUDED[-1]])
        self.assertEqual(re.findall(r'"([^"]+)"', listed), EXCLUDED[:-1])

    def test_the_lines_exist_only_with_the_variable_and_are_the_json_form_of_the_list(self):
        lines = locals_assignment("locals.tf", "k8s_log_exclusion_lines").strip()
        self.assertEqual(lines, 'var.pod_backend_enabled ? ["    exclude_namespaces = ${jsonencode(local.k8s_log_excluded_namespaces)}"] : []')

    def test_the_off_state_is_byte_identical_to_the_text_main_produced(self):
        self.assertEqual(render_log_settings(on=False), OFF_TEXT)

    def test_the_on_state_has_the_list_under_stdout_and_stderr_and_nowhere_else(self):
        text = render_log_settings(on=True)
        listing = '    exclude_namespaces = ["kube-system","gatekeeper-system","factory-workers"]'
        self.assertEqual(
            text.split("\n"),
            [
                "[log_collection_settings]",
                "  [log_collection_settings.stdout]",
                "    enabled = true",
                listing,
                "  [log_collection_settings.stderr]",
                "    enabled = true",
                listing,
                "  [log_collection_settings.env_var]",
                "    enabled = false",
            ],
        )
        self.assertEqual(text.count("exclude_namespaces"), 2)

    def test_the_exclusion_is_inserted_exactly_twice_and_the_environment_collection_stays_off(self):
        data = locals_assignment("locals.tf", "k8s_insights_agent_config")
        self.assertEqual(data.count("local.k8s_log_exclusion_lines"), 2)
        self.assertIn('"    enabled = false"', data)
        self.assertNotIn("exclude_namespaces", data)  # the key is spelled once, in the lines local


class RelayRuleOfThePreparedPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = resource("monitoring.tf", "kubernetes_manifest", "factory_network_policy")

    def test_the_relay_rule_is_inside_a_conditional_on_the_variable_and_nowhere_else(self):
        self.assertEqual(self.policy.count("var.pod_backend_enabled"), 1)
        self.assertEqual(self.policy.count("local.worker_contract.ports.relay"), 1)
        found = re.search(r"var\.pod_backend_enabled \? \[(.*?)\] : \[\]", self.policy, re.DOTALL)
        self.assertIsNotNone(found, "the relay rule is not behind `var.pod_backend_enabled ? [...] : []`")
        gated = found.group(1)
        self.assertIn("local.worker_contract.ports.relay", gated)
        self.assertIn("local.worker_contract.names.worker_namespace", gated)
        self.assertIn('"${local.worker_contract.labels.prefix}role" = local.worker_contract.labels.role_value', gated)
        self.assertIn('protocol = "TCP"', gated)

    def test_it_is_appended_after_the_two_rules_that_exist_today_so_no_other_entry_moves(self):
        squashed = squash(self.policy)
        self.assertRegex(squashed, r"ingress = concat\( \[ \{ from = .*? port = local\.app_port \}\] \}, \{ from = .*? port = local\.metrics_port \}\] \}, \], var\.pod_backend_enabled \? \[")

    def test_every_from_list_has_one_peer_that_selects_the_namespace_and_the_pod_together(self):
        # The same AND-versus-OR as the egress rules of the worker policy: `from = [{ns}, {pod}]` would admit every pod of the
        # namespace OR every pod with the label, from any namespace.
        lists = list_bodies(self.policy, r"\bfrom\s*=\s*\[")
        self.assertEqual(len(lists), 3, "the app rule, the metrics rule and the gated relay rule")
        for number, body in enumerate(lists):
            with self.subTest(rule=number):
                view = check_refs.blank_view(body)
                first = view.index("{")
                self.assertEqual(view[:first].strip(), "")
                self.assertEqual(view[check_refs.match_brace(view, first) :].strip(), "", "a second peer follows the first")
                self.assertEqual(body.count("namespaceSelector"), 1)
                self.assertEqual(body.count("podSelector"), 1)

    def test_the_two_rules_that_exist_today_are_outside_the_conditional(self):
        before = self.policy.split("var.pod_backend_enabled", 1)[0]
        self.assertIn("local.app_port", before)
        self.assertIn("local.metrics_port", before)

    def test_the_comment_says_when_the_rule_exists(self):
        raw = (INFRA / "monitoring.tf").read_text(encoding="utf-8")
        self.assertIn("pod_backend_enabled", raw.split('resource "kubernetes_manifest" "factory_network_policy"', 1)[0].rsplit("Network isolation", 1)[1])


class TheStaleSentenceIsRewritten(unittest.TestCase):
    HEADER = (INFRA / "k8s.tf").read_text(encoding="utf-8").split("\nlocals {", 1)[0]

    def test_a_policy_is_no_longer_called_decoration_and_the_header_says_what_enforces_one(self):
        flat = squash(self.HEADER.replace("# ", ""))
        self.assertNotRegex(flat, r"(?i)decoration")
        self.assertNotRegex(flat, r"(?i)runs no network policy\s+engine")
        self.assertIn('enforced only by the Cilium data plane (network_data_plane = "cilium"', flat)
        self.assertIn("worker_ns.tf declares the default-deny policy of the worker namespace", flat)
        self.assertIn("pod_backend_enabled", flat)
        self.assertIn("policy of the Factory namespace stays prepared and off", flat)

    def test_the_old_trailing_paragraph_that_promised_this_rewrite_is_gone(self):
        self.assertNotIn("is accurate only while the cluster runs no policy", self.HEADER)
        self.assertNotIn("rewrites this comment", self.HEADER)

    def test_the_comment_above_the_metrics_service_no_longer_says_the_cluster_has_no_engine(self):
        raw = (INFRA / "k8s.tf").read_text(encoding="utf-8")
        service = raw.split('resource "kubernetes_service_v1" "factory_metrics"', 1)[0].rsplit("# The metrics listener", 1)[1]
        self.assertNotIn("no network policy engine", squash(service.replace("# ", "")))
        self.assertIn("until the Cilium data plane is switched on", squash(service.replace("# ", "")))

    def test_the_variable_and_the_monitoring_comment_use_the_same_sentence(self):
        for text in (variable("factory_network_policy_enabled"), (INFRA / "monitoring.tf").read_text(encoding="utf-8")):
            self.assertIn("Enforcement needs the Cilium data plane", text)

    def test_the_header_of_the_new_file_names_the_control_and_the_unverified_items(self):
        header = (INFRA / "worker_ns.tf").read_text(encoding="utf-8").split("\nlocals {", 1)[0]
        flat = squash(header.replace("# ", ""))
        for needle in ("default-deny", "169.254.169.254", "168.63.129.16", "Cilium", "UNVERIFIED", "ipBlock.except", "exclude_namespaces", "NO RoleBinding"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)


if __name__ == "__main__":
    unittest.main()
