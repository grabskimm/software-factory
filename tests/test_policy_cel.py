"""The CEL of the worker admission policies, run offline against the golden pod, the golden Secrets and every mutation.

Slice 15b of the pod backend (``docs/pod-backend.md``). ``infra/policy/*.json`` holds the ValidatingAdmissionPolicy objects;
``tests/celmodel.py`` is a small model of the CEL subset they use. This module replays the files of slice 6
(``tests/golden/policy_mutations.json``, ``policy_secret_mutations.json``) through the model, after emulating what an API
server does to an object before validating admission (defaulting, the two tolerations of the DefaultTolerationSeconds
plugin, the reset of ``status``, the request defaulted from the limit, empty lists and plain false booleans dropped by the
typed round trip). A mutation passes the matrix when the CEL of ITS rule denies it, or when the server turns it into the
golden object (it is then not a mutation). The model is NOT evidence that a real API server accepts the expressions: that
is what a run of ``tools/policy_matrix.py`` against a kind cluster replays.

Every rule of the policies is mutation-tested here: removing a validation, or replacing its expression by ``true``, makes
the matrix report it (the last tests).
"""

import base64
import binascii
import copy
import importlib.util
import json
import re
import unittest
from pathlib import Path

from factory import podspec as ps
from tests import celmodel

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("policy_matrix_under_test", ROOT / "tools" / "policy_matrix.py")
matrix_tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(matrix_tool)
POLICY_DIR = ROOT / "infra" / "policy"
GOLDEN = ROOT / "tests" / "golden"
LAUNCHER = "system:serviceaccount:factory:factory"
REQUEST = {"userInfo": {"username": LAUNCHER}}
RULE = re.compile(r"^\[rule:([a-z]+(?:-[a-z]+)*)\] ")

# Mutations the model cannot evaluate because a real API server rejects the object while DECODING it (a value of the wrong
# JSON type, a base64 string that is not base64): the expression then errors, which on a real server is a denial without the
# rule id. The matrix tool classifies such a request as "rejected before the policy".
DECODE_IMPOSSIBLE = {"containers-empty", "deadline-boolean", "deadline-string", "grace-string"}
# A Secret whose data is not base64: the server rejects it while decoding.
DECODE_REJECTED = {"secret-value-not-base64"}
# Mutations after which the server's object is the golden object (the field is defaulted back, or dropped): not a mutation.
NO_EFFECT = {
    "grace-absent",
    "resources-no-ephemeral-request",
    "spec-topologySpreadConstraints",
    "metadata-status-supplied",
    "secret-type-absent",
}
FALSE_IS_OMITTED = {"hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace", "setHostnameAsFQDN", "stdin", "stdinOnce", "tty", "readOnly"}
NO_EXECUTE_KEYS = ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")
# Added by the API server to a pod that is not BestEffort (seen on AKS, issue #145 mirror run): the policy has to let it through.
MEMORY_PRESSURE = {"key": "node.kubernetes.io/memory-pressure", "operator": "Exists", "effect": "NoSchedule"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical(document):
    """Type-sensitive equality: in Python 1 == True, in JSON they differ."""
    return json.dumps(document, sort_keys=True)


def omit_empty(value):
    """What the typed JSON round trip drops: empty lists and plain (non-pointer) booleans that are false."""
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            item = omit_empty(item)
            if (isinstance(item, list) and not item) or (key in FALSE_IS_OMITTED and item is False):
                continue
            kept[key] = item
        return kept
    if isinstance(value, list):
        return [omit_empty(item) for item in value]
    return value


def _dicts(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def default_requests(resources):
    """The API server's defaulting: a limit without a request gives a request equal to it."""
    if not isinstance(resources, dict) or not isinstance(resources.get("limits"), dict):
        return
    requests = resources.setdefault("requests", {})
    if not isinstance(requests, dict):
        return
    for name, size in resources["limits"].items():
        requests.setdefault(name, size)


def server_pod(pod):
    """The pod as validating admission sees it (an emulation of the API server's defaulting and mutation, for this model)."""
    obj = omit_empty(copy.deepcopy(pod))
    meta = obj.setdefault("metadata", {})
    meta.setdefault("uid", "11111111-2222-4333-8444-555555555555")
    meta.setdefault("creationTimestamp", "2026-01-01T00:00:00Z")
    spec = obj.setdefault("spec", {})
    for key, default in (
        ("dnsPolicy", "ClusterFirst"),
        ("schedulerName", "default-scheduler"),
        ("securityContext", {}),
        ("priority", 0),
        ("preemptionPolicy", "PreemptLowerPriority"),
        ("restartPolicy", "Always"),
        ("terminationGracePeriodSeconds", 30),
        ("enableServiceLinks", True),
        ("serviceAccountName", "default"),
    ):
        spec.setdefault(key, default)
    if isinstance(spec.get("serviceAccountName"), str):
        spec.setdefault("serviceAccount", spec["serviceAccountName"])
    tolerations = spec.setdefault("tolerations", [])
    if isinstance(tolerations, list):
        present = {item.get("key") for item in _dicts(tolerations) if item.get("effect") == "NoExecute"}
        for key in NO_EXECUTE_KEYS:
            if key not in present:
                tolerations.append({"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300})
        if not any(item.get("key") == MEMORY_PRESSURE["key"] for item in _dicts(tolerations)):
            tolerations.append(dict(MEMORY_PRESSURE))
    for container in _dicts(spec.get("containers")) + _dicts(spec.get("initContainers")):
        container.setdefault("terminationMessagePath", "/dev/termination-log")
        container.setdefault("terminationMessagePolicy", "File")
        default_requests(container.get("resources"))
        for entry in _dicts(container.get("env")):
            field = entry.get("valueFrom", {}).get("fieldRef") if isinstance(entry.get("valueFrom"), dict) else None
            if isinstance(field, dict):
                field.setdefault("apiVersion", "v1")
    for volume in _dicts(spec.get("volumes")):
        if isinstance(volume.get("secret"), dict):
            volume["secret"].setdefault("defaultMode", 420)
    obj["status"] = {"phase": "Pending", "qosClass": "Guaranteed"}
    return obj


class DecodeRejected(Exception):
    """The API server cannot decode the object (the model's stand-in for its 400)."""


def server_secret(secret):
    obj = copy.deepcopy(secret)
    for value in (obj.get("data") or {}).values():
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, TypeError, ValueError) as error:
            raise DecodeRejected from error
    obj.setdefault("type", "Opaque")
    string_data = obj.pop("stringData", None)
    if isinstance(string_data, dict):
        data = obj.setdefault("data", {})
        for key, value in string_data.items():
            data[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")
    meta = obj.setdefault("metadata", {})
    meta.setdefault("uid", "99999999-2222-4333-8444-555555555555")
    return obj


def policies():
    return load(POLICY_DIR / "pod-policy.json"), load(POLICY_DIR / "secret-policy.json")


def rule_of(validation):
    found = RULE.match(validation["message"])
    if found is None:
        raise AssertionError("a validation message must start with [rule:<id>]")
    return found.group(1)


def verdict(policy, obj, params):
    """(rule ids denied, rule ids whose expression raised): an error denies, as failurePolicy Fail does on a real server."""
    for condition in policy["spec"]["matchConditions"]:
        if celmodel.evaluate(condition["expression"], request=REQUEST) is not True:
            return [], []
    denied, errors = [], []
    for validation in policy["spec"]["validations"]:
        rule = rule_of(validation)
        try:
            passed = celmodel.evaluate(validation["expression"], object=obj, params=params, request=REQUEST)
        except celmodel.CelError:
            errors.append(rule)
            denied.append(rule)
            continue
        if passed is not True:
            denied.append(rule)
    return denied, errors


def named_rule_denied(mutation, denied):
    """The mutation's own rule denies it, or a rule the API server shows instead (tools/policy_matrix.py SERVER_VIEW)."""
    named = {mutation["rule"], *matrix_tool.SERVER_VIEW.get(mutation["id"], ())}
    return bool(named & set(denied))


def pod_params(golden):
    return {"data": {"image": golden["spec"]["containers"][0]["image"]}}


def matrix(pod_policy, secret_policy, only_rule=None):
    """(problems, stats): every golden object admitted, every mutation denied by its own rule or a no-op after defaulting.

    `only_rule` restricts the mutations to those of one rule (the golden objects are always checked).
    """
    problems, stats = [], {"no_effect": set(), "errors": set(), "rejected": set()}
    pod_file = load(GOLDEN / "policy_mutations.json")
    secret_file = load(GOLDEN / "policy_secret_mutations.json")
    golden = load(GOLDEN / pod_file["golden"])
    params = pod_params(golden)
    for path in sorted(GOLDEN.glob("pod_*.json")):
        denied, _ = verdict(pod_policy, server_pod(load(path)), params)
        if denied:
            problems.append(f"golden {path.name} denied by {denied}")
    for path in sorted(GOLDEN.glob("secret_*.json")):
        denied, _ = verdict(secret_policy, server_secret(load(path)), None)
        if denied:
            problems.append(f"golden {path.name} denied by {denied}")
    baseline = server_pod(golden)
    for mutation in pod_file["mutations"]:
        if only_rule and mutation["rule"] != only_rule:
            continue
        obj = server_pod(ps.apply_patch(golden, mutation["patch"]))
        if canonical(obj) == canonical(baseline):
            stats["no_effect"].add(mutation["id"])
            continue
        denied, errors = verdict(pod_policy, obj, params)
        if errors:
            stats["errors"].add(mutation["id"])
        if not named_rule_denied(mutation, denied):
            problems.append(f"pod mutation {mutation['id']} not denied by {mutation['rule']} (denied: {denied})")
    secret_golden = load(GOLDEN / secret_file["golden"])
    secret_baseline = server_secret(secret_golden)
    for mutation in secret_file["mutations"]:
        if only_rule and mutation["rule"] != only_rule:
            continue
        try:
            obj = server_secret(ps.apply_patch(secret_golden, mutation["patch"]))
        except DecodeRejected:
            stats["rejected"].add(mutation["id"])
            continue
        if canonical(obj) == canonical(secret_baseline):
            stats["no_effect"].add(mutation["id"])
            continue
        denied, errors = verdict(secret_policy, obj, None)
        if errors:
            stats["errors"].add(mutation["id"])
        if not named_rule_denied(mutation, denied):
            problems.append(f"secret mutation {mutation['id']} not denied by {mutation['rule']} (denied: {denied})")
    return problems, stats


def without(policy, rule, replacement=None):
    """A copy of `policy` with the validation of `rule` removed, or its expression replaced."""
    copied = copy.deepcopy(policy)
    kept = []
    for validation in copied["spec"]["validations"]:
        if rule_of(validation) != rule:
            kept.append(validation)
        elif replacement is not None:
            validation["expression"] = replacement
            kept.append(validation)
    copied["spec"]["validations"] = kept
    return copied


class PolicyCelModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pod_policy, cls.secret_policy = policies()
        cls.problems, cls.stats = matrix(cls.pod_policy, cls.secret_policy)

    def test_the_policies_carry_exactly_the_rule_ids_of_the_mirror_in_its_order(self):
        self.assertEqual([rule_of(v) for v in self.pod_policy["spec"]["validations"]], list(ps.POD_RULES))
        self.assertEqual([rule_of(v) for v in self.secret_policy["spec"]["validations"]], list(ps.SECRET_RULES))

    def test_every_validation_is_a_deny_with_a_message_and_a_reason_and_a_valid_expression(self):
        for policy in (self.pod_policy, self.secret_policy):
            for validation in policy["spec"]["validations"]:
                with self.subTest(rule=rule_of(validation)):
                    self.assertEqual(set(validation), {"expression", "message", "reason"})
                    self.assertEqual(validation["reason"], "Forbidden")
                    self.assertNotIn("\\", validation["expression"], "CEL string escapes are not used: spell a literal dot as [.]")
                    celmodel.compile_expression(validation["expression"])

    def test_every_golden_pod_and_secret_is_admitted_and_every_mutation_is_denied_by_its_rule(self):
        self.assertEqual(self.problems, [])

    def test_the_matrix_is_not_vacuous(self):
        pod_file = load(GOLDEN / "policy_mutations.json")
        secret_file = load(GOLDEN / "policy_secret_mutations.json")
        total = len(pod_file["mutations"]) + len(secret_file["mutations"])
        self.assertGreater(total, 200)
        self.assertLess(len(self.stats["no_effect"]), total // 10)

    def test_the_mutations_a_server_turns_back_into_the_golden_object_are_the_known_ones(self):
        self.assertEqual(self.stats["no_effect"], NO_EFFECT)

    def test_an_expression_raises_only_for_a_value_a_real_server_rejects_while_decoding_it(self):
        self.assertEqual(self.stats["errors"], DECODE_IMPOSSIBLE)
        self.assertEqual(self.stats["rejected"], DECODE_REJECTED)

    def test_the_server_view_names_only_rules_and_mutations_that_exist(self):
        mutations = {m["id"]: m for m in load(GOLDEN / "policy_secret_mutations.json")["mutations"]}
        for name, rules in matrix_tool.SERVER_VIEW.items():
            with self.subTest(name=name):
                self.assertIn(name, mutations)
                self.assertTrue(set(rules) <= set(ps.SECRET_RULES))
                self.assertNotIn(mutations[name]["rule"], rules)

    def test_no_expression_raises_on_a_golden_object(self):
        golden = load(GOLDEN / "pod_standard_build.json")
        self.assertEqual(verdict(self.pod_policy, server_pod(golden), pod_params(golden)), ([], []))
        secret = load(GOLDEN / "secret_standard_build.json")
        self.assertEqual(verdict(self.secret_policy, server_secret(secret), None), ([], []))

    def test_a_request_from_another_identity_is_not_judged(self):
        golden = load(GOLDEN / "pod_standard_build.json")
        broken = copy.deepcopy(golden)
        broken["spec"]["hostNetwork"] = True
        other = copy.deepcopy(self.pod_policy)
        other["spec"]["matchConditions"][0]["expression"] = "request.userInfo.username == 'system:serviceaccount:factory:other'"
        self.assertEqual(verdict(other, server_pod(broken), pod_params(golden)), ([], []))
        self.assertIn("host-network", verdict(self.pod_policy, server_pod(broken), pod_params(golden))[0])

    def test_the_pod_policy_refuses_without_a_valid_image_parameter(self):
        golden = load(GOLDEN / "pod_standard_build.json")
        obj = server_pod(golden)
        for params in ({"data": {}}, {"data": {"image": "registry.example.invalid/factory:latest"}}, {"data": {"image": f"registry.example.invalid/factory@sha256:{'f' * 64}"}}):
            with self.subTest(params=params):
                self.assertIn("image", verdict(self.pod_policy, obj, params)[0])

    def test_cases_beyond_the_golden_matrix_are_denied_by_their_rule(self):
        golden = load(GOLDEN / "pod_standard_build.json")
        worker = golden["spec"]["tolerations"][0]
        term = golden["spec"]["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
        required = "/spec/affinity/podAntiAffinity/requiredDuringSchedulingIgnoredDuringExecution"
        cases = {
            "a duplicated worker toleration": ([{"op": "add", "path": "/spec/tolerations/-", "value": worker}], "tolerations"),
            "a worker toleration with a toleration time": ([{"op": "add", "path": "/spec/tolerations/0/tolerationSeconds", "value": 60}], "tolerations"),
            "a memory-pressure toleration with a toleration time": ([{"op": "add", "path": "/spec/tolerations/-", "value": {**MEMORY_PRESSURE, "tolerationSeconds": 60}}], "tolerations"),
            "a toleration of another condition taint": ([{"op": "add", "path": "/spec/tolerations/-", "value": {**MEMORY_PRESSURE, "key": "node.kubernetes.io/disk-pressure"}}], "tolerations"),
            "a second anti-affinity term": ([{"op": "add", "path": f"{required}/-", "value": term}], "anti-affinity"),
            "a data volume of the other size": ([{"op": "replace", "path": "/spec/volumes/1/emptyDir/sizeLimit", "value": "3Gi"}], "volumes"),
        }
        for name, (patch, rule) in cases.items():
            with self.subTest(name=name):
                denied, _ = verdict(self.pod_policy, server_pod(ps.apply_patch(golden, patch)), pod_params(golden))
                self.assertIn(rule, denied)

    def test_removing_any_one_rule_is_caught_by_the_matrix(self):
        for rule in (*ps.POD_RULES, *ps.SECRET_RULES):
            pod_policy, secret_policy = self.pod_policy, self.secret_policy
            if rule in ps.POD_RULES:
                pod_policy = without(pod_policy, rule)
            else:
                secret_policy = without(secret_policy, rule)
            with self.subTest(rule=rule):
                problems, _ = matrix(pod_policy, secret_policy, only_rule=rule)
                self.assertTrue(any(f"by {rule} " in problem for problem in problems), "the matrix did not notice the missing rule")

    def test_a_rule_that_admits_everything_is_caught_by_the_matrix(self):
        for rule in (*ps.POD_RULES, *ps.SECRET_RULES):
            pod_policy, secret_policy = self.pod_policy, self.secret_policy
            if rule in ps.POD_RULES:
                pod_policy = without(pod_policy, rule, "true")
            else:
                secret_policy = without(secret_policy, rule, "true")
            with self.subTest(rule=rule):
                problems, _ = matrix(pod_policy, secret_policy, only_rule=rule)
                self.assertTrue(any(f"by {rule} " in problem for problem in problems), "the matrix did not notice the empty rule")


class CelModelSemantics(unittest.TestCase):
    def test_missing_fields_are_errors_and_has_tests_a_field(self):
        with self.assertRaises(celmodel.CelError):
            celmodel.evaluate("object.a.b", object={"a": {}})
        self.assertIs(celmodel.evaluate("has(object.a.b)", object={"a": {}}), False)
        self.assertIs(celmodel.evaluate("has(object.a.b)", object={"a": {"b": 1}}), True)
        with self.assertRaises(celmodel.CelError):
            celmodel.evaluate("has(object.a.b)", object={})

    def test_and_or_absorb_an_error_on_the_other_side(self):
        self.assertIs(celmodel.evaluate("object.x.y == 1 && false", object={}), False)
        self.assertIs(celmodel.evaluate("false && object.x.y == 1", object={}), False)
        self.assertIs(celmodel.evaluate("true || object.x.y == 1", object={}), True)
        with self.assertRaises(celmodel.CelError):
            celmodel.evaluate("true && object.x.y == 1", object={})

    def test_equality_tells_a_boolean_from_a_number_and_ordering_needs_numbers(self):
        self.assertIs(celmodel.evaluate("true == 1"), False)
        self.assertIs(celmodel.evaluate("1 == 1.0"), True)
        with self.assertRaises(celmodel.CelError):
            celmodel.evaluate("true >= 1")
        with self.assertRaises(celmodel.CelError):
            celmodel.evaluate("'1' >= 1")

    def test_macros_over_lists_and_map_keys(self):
        env = {"object": {"l": [1, 2, 3], "m": {"a": 1, "b": 2}}}
        self.assertIs(celmodel.evaluate("object.l.all(x, x > 0)", **env), True)
        self.assertIs(celmodel.evaluate("object.l.exists(x, x > 2)", **env), True)
        self.assertIs(celmodel.evaluate("object.l.exists_one(x, x > 2)", **env), True)
        self.assertIs(celmodel.evaluate("object.l.exists_one(x, x > 1)", **env), False)
        self.assertEqual(celmodel.evaluate("object.l.filter(x, x > 1)", **env), [2, 3])
        self.assertIs(celmodel.evaluate("object.m.all(k, size(object.m[k]) == 0 || object.m[k] > 0)", **env), True)
        self.assertIs(celmodel.evaluate("'a' in object.m && !('c' in object.m) && 2 in object.l", **env), True)

    def test_quantities_strings_and_the_ternary(self):
        self.assertIs(celmodel.evaluate("quantity('4Gi').compareTo(quantity('4096Mi')) == 0"), True)
        self.assertIs(celmodel.evaluate("quantity('1e3').isGreaterThan(quantity('999'))"), True)
        self.assertIs(celmodel.evaluate("isQuantity('lots')"), False)
        self.assertIs(celmodel.evaluate("'ab'.matches('^a') && !'ab'.matches('^b$') && ('x' + 'y') == 'xy'"), True)
        self.assertEqual(celmodel.evaluate("true ? 1 : false ? 2 : 3"), 1)
        self.assertEqual(celmodel.evaluate("{'a': 'b'}['a']"), "b")

    def test_a_syntax_error_is_reported(self):
        for text in ("a &&", "(1", "1 +", "x.", "'open"):
            with self.subTest(text=text), self.assertRaises(celmodel.CelError):
                celmodel.compile_expression(text)


if __name__ == "__main__":
    unittest.main()
