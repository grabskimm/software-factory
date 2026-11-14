"""The README's description of the custom plan-identity role must match the shipped role definition.

Run from the repository root:  python3 -I -m unittest discover -s infra/test -v
Offline, standard library only.
"""

import json
import re
import unittest
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent
ROLE_JSON = INFRA / "roles" / "factory-plan-cluster-reader.json"
README = INFRA / "README.md"

ROLE_NAME = "Factory Plan Cluster Reader"
DATA_ACTIONS = {
    "Microsoft.ContainerService/managedClusters/apiextensions.k8s.io/customresourcedefinitions/read",
    "Microsoft.ContainerService/managedClusters/storage.k8s.io/storageclasses/read",
    "Microsoft.ContainerService/managedClusters/customresources/read",
    "Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterroles/read",
    "Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/clusterrolebindings/read",
    # Slice 15b (issue #144): the first plan after pod_backend_enabled is turned on reads the Role and RoleBinding of the
    # worker namespace. Both names are in the public Azure permissions reference for Microsoft.ContainerService.
    "Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/roles/read",
    "Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/rolebindings/read",
}
# NOT added, and why: the kinds `validatingadmissionpolicies` and `validatingadmissionpolicybindings` of
# `admissionregistration.k8s.io` have NO data action in the public Azure permissions reference (it lists the webhook
# configurations and the API discovery paths of that group, not these two). A role definition that names an action the
# provider does not define is refused by `az role definition create`, which would block the two names above as well.
# Whether the Azure authorizer maps the two kinds to `customresources/read` (already granted) is UNVERIFIED.
UNDEFINED_DATA_ACTION_WORDS = ("validatingadmissionpolic", "admissionregistration")


def plan_row(text):
    rows = [line for line in text.splitlines() if line.lstrip().startswith("| **Plan**")]
    if len(rows) != 1:
        raise AssertionError(f"expected exactly one plan row in the identity table, found {len(rows)}")
    return rows[0]


class PlanRoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.role = json.loads(ROLE_JSON.read_text(encoding="utf-8"))
        cls.readme = README.read_text(encoding="utf-8")

    def test_json_is_exactly_the_seven_read_data_actions(self):
        role = self.role
        self.assertEqual(role["Name"], ROLE_NAME)
        self.assertTrue(role["IsCustom"])
        self.assertEqual(sorted(role["DataActions"]), sorted(DATA_ACTIONS))
        self.assertEqual(len(role["DataActions"]), len(set(role["DataActions"])))
        for key in ("Actions", "NotActions", "NotDataActions"):
            self.assertEqual(role[key], [], key)
        for action in role["DataActions"]:
            self.assertTrue(action.endswith("/read"), action)
        self.assertEqual(len(role["AssignableScopes"]), 1)
        self.assertRegex(role["AssignableScopes"][0], r"^/subscriptions/[^/]+/resourceGroups/[^/]+$")

    def test_no_data_action_is_named_that_the_azure_reference_does_not_define(self):
        for action in self.role["DataActions"]:
            for word in UNDEFINED_DATA_ACTION_WORDS:
                self.assertNotIn(word, action, "this kind has no documented data action: see the comment above")

    def test_the_role_reads_roles_and_role_bindings_of_a_namespace_as_well_as_the_cluster_ones(self):
        actions = set(self.role["DataActions"])
        for kind in ("roles", "rolebindings", "clusterroles", "clusterrolebindings"):
            self.assertIn(f"Microsoft.ContainerService/managedClusters/rbac.authorization.k8s.io/{kind}/read", actions)

    def test_readme_plan_row_names_the_role(self):
        row = plan_row(self.readme)
        self.assertIn(f"`{self.role['Name']}`", row)
        self.assertIn("infra/roles/factory-plan-cluster-reader.json", row)
        for built_in in ("Azure Kubernetes Service Cluster User Role", "Azure Kubernetes Service RBAC Reader"):
            self.assertIn(built_in, row)

    def test_readme_lists_the_same_data_actions_as_the_json(self):
        documented = set(re.findall(r"Microsoft\.ContainerService/managedClusters/[A-Za-z0-9./_-]+/read", self.readme))
        self.assertEqual(documented, set(self.role["DataActions"]))

    def test_readme_bootstrap_creates_and_assigns_the_role_from_the_json(self):
        self.assertIn("az role definition create --role-definition", self.readme)
        self.assertIn("infra/roles/factory-plan-cluster-reader.json", self.readme)
        self.assertNotIn("$TMPDIR", self.readme, "TMPDIR is often unset; use mktemp")
        self.assertIn("ROLE_FILE=$(mktemp)", self.readme)
        assign = re.search(r"az role assignment create[^`]*?--role \"([^\"]+)\"[^`]*?--scope \"[^\"]*resourceGroups", self.readme)
        self.assertIsNotNone(assign, "the bootstrap steps must assign the role on the resource group")
        self.assertEqual(assign.group(1), self.role["Name"])

    def test_readme_states_the_scoping_and_secret_caveats(self):
        flat = " ".join(self.readme.split())
        self.assertIn("`customresources/read` cannot be scoped per CRD", flat)
        self.assertIn("exposes no secret values", flat)

    def test_the_role_name_is_not_documented_under_another_spelling(self):
        names = set(re.findall(r"Factory Plan Cluster \w+", self.readme))
        self.assertEqual(names, {ROLE_NAME})


if __name__ == "__main__":
    unittest.main()
