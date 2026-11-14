"""tools/policy_matrix.py against a fake kubectl (slice 15b of the pod backend, docs/pod-backend.md).

The tool replays the golden pods and Secrets and every mutation against a real API server (a kind cluster, tools/local_pod_e2e/). Here the
"server" is `FakeCluster`: a stand-in for `subprocess.run` that decodes the request like kubectl would send it, applies the
emulation of the API server's defaulting from tests/test_policy_cel.py and answers with the CEL model's verdict in the format of
the real denial message. Nothing here calls a cluster, a network or a real kubectl, and nothing sleeps (the pause is injected).
What these tests prove is the TOOL: the classification of each answer, what makes the run fail, that it only passes argv
lists with a timeout, and what it prints. They do not prove the CEL (tests/test_policy_cel.py runs it through a model; a run
against a real API server does).
"""

import ast
import copy
import json
import subprocess
import unittest

from tests import test_policy_cel as tc

tool = tc.matrix_tool
ROOT = tc.ROOT
TOOL_PATH = ROOT / "tools" / "policy_matrix.py"
PLACEHOLDERS = ("PGFudGhyb3BpYy1hcGkta2V5Pg==", "PHJlbGF5LXRva2VuPg==")


class FakeCluster:
    """A callable with the signature of subprocess.run that behaves like `kubectl create --dry-run=server` on the model."""

    def __init__(self, pod_policy=None, secret_policy=None, rewrite=None):
        policies = tc.policies()
        self.pod_policy = pod_policy or policies[0]
        self.secret_policy = secret_policy or policies[1]
        golden = tc.load(tc.GOLDEN / "pod_standard_build.json")
        self.params = tc.pod_params(golden)
        self.rewrite = rewrite
        self.calls = []
        self.status = None

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None, check=False, **extra):
        self.calls.append({"argv": list(argv), "input": input, "timeout": timeout, "extra": extra})
        if "get" in argv:
            return self.get(argv)
        return self.create(json.loads(input))

    def done(self, code, out="", err=""):
        return subprocess.CompletedProcess([], code, out, err)

    def bad_request(self, text="cannot decode the object"):
        return self.done(1, err=f"Error from server (BadRequest): error when creating \"STDIN\": {text}")

    def create(self, manifest):
        kind = manifest.get("kind")
        if kind not in ("Pod", "Secret") or manifest.get("apiVersion") != "v1":
            return self.done(1, err='error: no matches for kind "x" in version "v"')
        try:
            if kind == "Pod":
                obj, policy, params = tc.server_pod(manifest), self.pod_policy, self.params
            else:
                obj, policy, params = tc.server_secret(manifest), self.secret_policy, None
        except tc.DecodeRejected:
            return self.bad_request()
        denied, errors = tc.verdict(policy, obj, params)
        if errors:
            return self.bad_request()
        if denied:
            name = policy["metadata"]["name"]
            message = next(v["message"] for v in policy["spec"]["validations"] if tc.rule_of(v) == denied[0])
            if self.rewrite:
                name, message = self.rewrite(name, message)
            return self.done(1, err=f"Error from server (Forbidden): error when creating \"STDIN\": ValidatingAdmissionPolicy '{name}' with binding '{name}' denied request: {message}")
        return self.done(0, out=json.dumps(obj))

    def get(self, argv):
        if self.status is None:
            return self.done(1, err="Error from server (NotFound)")
        return self.done(0, out=json.dumps(self.status(argv[argv.index("validatingadmissionpolicy") + 1])))


def run(cluster, **options):
    lines, pauses = [], []
    kubectl = tool.Kubectl("kubectl", tool.DEFAULT_USER, 30, options.get("kubeconfig"), cluster)
    problems = tool.run_matrix(kubectl, lines.append, pauses.append)
    return problems, lines, pauses


class MatrixAgainstTheModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = FakeCluster()
        cls.problems, cls.lines, cls.pauses = run(cls.cluster)

    def test_the_real_policies_pass_with_no_problem_and_no_pause(self):
        self.assertEqual(self.problems, 0, [line for line in self.lines if line.startswith("FAIL")])
        self.assertEqual(self.pauses, [], "the policy was enforced at once: the tool waits only when it must")

    def test_the_summary_counts_every_golden_and_every_mutation(self):
        pod_file = tc.load(tc.GOLDEN / "policy_mutations.json")
        secret_file = tc.load(tc.GOLDEN / "policy_secret_mutations.json")
        total = len(pod_file["mutations"]) + len(secret_file["mutations"])
        summary = self.lines[-1]
        goldens = len(list(tc.GOLDEN.glob("pod_*.json"))) + len(list(tc.GOLDEN.glob("secret_*.json")))
        self.assertIn(f"{goldens} golden admitted", summary)
        self.assertIn(f"{len(tc.NO_EFFECT)} no effect", summary)
        self.assertTrue(summary.endswith(f"0 problems, {total} mutations"), summary)
        denied = sum(1 for line in self.lines if line.startswith("mutation ") and ": denied by " in line)
        exercised = denied + len(tc.NO_EFFECT)
        not_exercised = sum(1 for line in self.lines if "not exercised" in line and line.startswith("mutation"))
        self.assertEqual(exercised + not_exercised, total)
        # The kinds and versions a real server cannot decode, the five the model cannot decode and nothing else.
        self.assertEqual(not_exercised, len(tc.DECODE_IMPOSSIBLE) + len(tc.DECODE_REJECTED) + 3)

    def test_a_mutation_the_server_turns_back_into_the_golden_object_is_not_a_mutation(self):
        for name in tc.NO_EFFECT:
            self.assertIn(f"mutation {name}: no effect (the server turned it back into the golden object)", self.lines)

    def test_every_call_is_an_argv_list_with_a_timeout_the_identity_and_the_dry_run(self):
        creates = [call for call in self.cluster.calls if "create" in call["argv"]]
        self.assertGreater(len(creates), 200)
        for call in creates:
            self.assertIsInstance(call["argv"], list)
            self.assertEqual(call["argv"][:3], ["kubectl", "--as", tool.DEFAULT_USER])
            self.assertEqual(call["argv"][3:], ["create", "--dry-run=server", "-f", "-", "-o", "json"])
            self.assertEqual(call["timeout"], 30)
            self.assertEqual(call["extra"], {})
            self.assertTrue(call["input"].startswith("{"))

    def test_the_kubeconfig_is_passed_as_an_argument_when_given(self):
        cluster = FakeCluster()
        kubectl = tool.Kubectl("kubectl", "u", 5, "/x/kubeconfig", cluster)
        kubectl.get_json("validatingadmissionpolicy", "p")
        self.assertEqual(cluster.calls[0]["argv"], ["kubectl", "--kubeconfig", "/x/kubeconfig", "--as", "u", "get", "validatingadmissionpolicy", "p", "-o", "json"])

    def test_nothing_it_prints_is_a_manifest_a_response_or_a_secret_value(self):
        text = "\n".join(self.lines)
        for placeholder in PLACEHOLDERS:
            self.assertNotIn(placeholder, text)
        for forbidden in ("apiVersion", "ownerReferences", "sha256:", "anthropic-api-key", '"'):
            self.assertNotIn(forbidden, text)
        for line in self.lines:
            self.assertRegex(line, r"^(golden |mutation |matrix: |FAIL )")


class WhatMakesTheRunFail(unittest.TestCase):
    def failures(self, **options):
        problems, lines, pauses = run(options.pop("cluster"))
        return problems, [line for line in lines if line.startswith("FAIL")], lines, pauses

    def test_a_rule_that_is_missing_from_the_policy_leaves_its_mutation_admitted(self):
        pod_policy, _ = tc.policies()
        broken = tc.without(pod_policy, "host-network")
        problems, fails, _, _ = self.failures(cluster=FakeCluster(pod_policy=broken))
        self.assertGreater(problems, 0)
        self.assertIn("FAIL mutation host-network-true was ADMITTED", fails)

    def test_a_golden_object_that_is_denied_fails(self):
        _, secret_policy = tc.policies()
        broken = tc.without(secret_policy, "secret-type", "false")
        problems, fails, _, _ = self.failures(cluster=FakeCluster(secret_policy=broken))
        self.assertGreater(problems, 0)
        self.assertTrue(any(line.startswith("FAIL golden secret_") and "was not admitted (policy" in line for line in fails), fails)

    def test_a_denial_without_a_rule_id_fails(self):
        cluster = FakeCluster(rewrite=lambda name, message: (name, "denied without an id"))
        _, fails, _, _ = self.failures(cluster=cluster)
        self.assertIn("FAIL mutation host-network-true: denied by the policy without a rule id", fails)

    def test_a_denial_by_another_policy_fails(self):
        cluster = FakeCluster(rewrite=lambda name, message: ("some-other-policy", message))
        _, fails, _, _ = self.failures(cluster=cluster)
        self.assertIn("FAIL mutation host-network-true: denied by another policy", fails)

    def test_a_denial_naming_a_rule_that_does_not_fit_the_mutation_fails(self):
        cluster = FakeCluster(rewrite=lambda name, message: (name, "[rule:image] wrong"))
        _, fails, _, _ = self.failures(cluster=cluster)
        self.assertTrue(any(line.startswith("FAIL mutation host-network-true: denied naming image, expected one of host-network") for line in fails), fails)

    def test_a_policy_that_never_starts_to_deny_fails_after_a_bounded_wait(self):
        pod_policy, _ = tc.policies()
        empty = copy.deepcopy(pod_policy)
        empty["spec"]["validations"] = []
        problems, fails, lines, pauses = self.failures(cluster=FakeCluster(pod_policy=empty))
        self.assertEqual(problems, 1)
        self.assertEqual(fails, ["FAIL the pod policy did not start to deny within the allowed time"])
        self.assertEqual(pauses, [tool.PAUSE_SECONDS] * tool.ATTEMPTS)
        self.assertEqual(len(lines), 2, "no golden or mutation is submitted before the policy denies")
        self.assertTrue(60 <= tool.ATTEMPTS * tool.PAUSE_SECONDS <= 120, "the wait is bounded: one to two minutes")

    def test_a_timeout_is_a_failure_not_a_pass(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        problems, _, _, pauses = self.failures(cluster=hang)
        self.assertEqual(problems, 1)
        self.assertEqual(len(pauses), tool.ATTEMPTS)

    def test_a_missing_kubectl_is_a_failure(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        problems, _, _, _ = self.failures(cluster=missing)
        self.assertEqual(problems, 1)

    def test_too_many_mutations_that_never_reach_the_policy_fails_the_run(self):
        inner = FakeCluster()
        golden = tc.load(tc.GOLDEN / "pod_standard_build.json")
        probe = tool.probe_pod(golden)
        known = [tc.load(path) for path in sorted(tc.GOLDEN.glob("*_*.json")) if path.name.startswith(("pod_", "secret_"))]
        known.append(probe)

        def reject_every_mutation(argv, **kwargs):
            if json.loads(kwargs["input"]) in known:
                return inner(argv, **kwargs)
            return inner.bad_request()

        problems, fails, lines, _ = self.failures(cluster=reject_every_mutation)
        self.assertEqual(fails, [f"FAIL more than {tool.MAX_NOT_EXERCISED_PERCENT} percent of the mutations never reached the policy"])
        self.assertEqual(problems, 1)
        self.assertGreater(sum(1 for line in lines if "not exercised" in line and line.startswith("mutation")), 200)


class Classification(unittest.TestCase):
    def test_an_admitted_answer_carries_the_document_and_an_unreadable_one_is_its_own_class(self):
        self.assertEqual(tool.classify(tool.Outcome(0, '{"a": 1}')).document, {"a": 1})
        self.assertEqual(tool.classify(tool.Outcome(0, "not json")).kind, "unreadable")

    def test_a_policy_denial_yields_the_policy_and_every_rule_token_of_the_message(self):
        err = "Error from server (Forbidden): ... ValidatingAdmissionPolicy 'p-1' with binding 'b' denied request: [rule:host-pid] a [rule:image] b"
        result = tool.classify(tool.Outcome(1, "", err))
        self.assertEqual((result.kind, result.policy, result.rules), ("policy", "p-1", ("host-pid", "image")))

    def test_another_rejection_is_sanitised_and_withheld_for_a_secret(self):
        err = "Error from server (Invalid): Pod \"x\" is invalid: spec.containers: Required value\nsecond line"
        result = tool.classify(tool.Outcome(1, "", err))
        self.assertEqual(result.kind, "other")
        self.assertNotIn("second line", result.detail)
        self.assertLessEqual(len(result.detail), tool.DETAIL_LIMIT)
        self.assertEqual(tool.classify(tool.Outcome(1, "", err), secret=True).detail, "details withheld")
        nasty = tool.classify(tool.Outcome(1, "", "bad \x1b[31m <script>$(x)</script> `y`"))
        self.assertNotRegex(nasty.detail, r"[<>$`\x1b]")

    def test_a_timeout_is_its_own_class(self):
        self.assertEqual(tool.classify(tool.Outcome(124, timed_out=True)).kind, "timeout")

    def test_comparable_ignores_the_uid_and_the_timestamps_and_tells_a_changed_spec(self):
        base = {"metadata": {"name": "a", "uid": "1", "creationTimestamp": "t"}, "spec": {"x": 1}}
        other = {"metadata": {"name": "a", "uid": "2", "creationTimestamp": "u"}, "spec": {"x": 1}}
        changed = {"metadata": {"name": "a"}, "spec": {"x": True}}
        self.assertEqual(tool.comparable(base), tool.comparable(other))
        self.assertNotEqual(tool.comparable(base), tool.comparable(changed), "1 and true are different values")
        self.assertIsNone(tool.comparable([]))


class StatusAndFixtures(unittest.TestCase):
    def status(self, warnings=(), observed=True):
        def answer(name):
            doc = {"metadata": {"name": name, "generation": 2}}
            if observed:
                doc["status"] = {"observedGeneration": 2, "typeChecking": {"expressionWarnings": list(warnings)}}
            return doc

        return answer

    def check(self, **options):
        cluster = FakeCluster()
        cluster.status = self.status(**options)
        lines, pauses = [], []
        problems = tool.check_status(tool.Kubectl("kubectl", "u", 5, None, cluster), lines.append, pauses.append)
        return problems, lines, pauses

    def test_no_warning_passes(self):
        problems, lines, pauses = self.check()
        self.assertEqual((problems, pauses), (0, []))
        self.assertEqual(lines, ["policy factory-worker-pods: 0 type-check warnings", "policy factory-worker-secrets: 0 type-check warnings"])

    def test_a_type_check_warning_fails_and_is_sanitised(self):
        problems, lines, _ = self.check(warnings=[{"fieldRef": "spec.validations[3].expression", "warning": "undefined field 'x' <b>"}])
        self.assertEqual(problems, 2)
        self.assertIn("FAIL spec.validations[3].expression: undefined field 'x' ?b?", lines)
        self.assertEqual(len([line for line in lines if line.startswith("FAIL")]), 2)

    def test_the_three_known_quantity_limits_of_the_type_checker_are_tolerated_and_anything_else_still_fails(self):
        known = [
            {"fieldRef": "spec.validations[15].expression", "warning": "/v1, Kind=Pod: ERROR: <input>:1:133: undefined field 'sizeLimit'\n | has(...)"},
            {"fieldRef": "spec.validations[26].expression", "warning": "/v1, Kind=Pod: ERROR: <input>:1:54: undefined field 'requests'\n | ..."},
            {"fieldRef": "spec.validations[36].expression", "warning": "/v1, Kind=Pod: ERROR: <input>:1:335: undefined field 'overhead'\n | ..."},
        ]
        problems, lines, _ = self.check(warnings=known)
        self.assertEqual(problems, 3)  # tolerated on the pod policy only: the Secret policy has none to tolerate, so the same text fails there
        self.assertEqual(len([line for line in lines if line.startswith("tolerated spec.validations")]), 3)
        self.assertEqual(len([line for line in lines if line.startswith("FAIL")]), 3)
        other = {"fieldRef": "spec.validations[1].expression", "warning": "/v1, Kind=Pod: ERROR: <input>:1:5: undefined field 'hostNetwork'"}
        same_name_other_kind = {"fieldRef": "spec.validations[2].expression", "warning": "/v1, Kind=Secret: ERROR: <input>:1:5: undefined field 'requests'"}
        problems, lines, _ = self.check(warnings=[other, same_name_other_kind])
        self.assertEqual(problems, 4)  # both policies, both warnings
        self.assertFalse([line for line in lines if line.startswith("tolerated")])

    def test_a_second_real_error_in_a_tolerated_warning_still_fails(self):
        two = {
            "fieldRef": "spec.validations[15].expression",
            "warning": "/v1, Kind=Pod: ERROR: <input>:1:40: undefined field 'sizeLimit'\n | has(x)\n | ...^\nERROR: <input>:1:90: undefined field 'imag'\n | y\n | ...^",
        }
        problems, lines, _ = self.check(warnings=[two])
        self.assertEqual(problems, 2)  # the pod policy and the Secret policy
        self.assertFalse([line for line in lines if line.startswith("tolerated")])
        both_known = {**two, "warning": two["warning"].replace("'imag'", "'limits'")}  # requests and limits: the resources rule names both
        _, lines, _ = self.check(warnings=[both_known])
        self.assertTrue([line for line in lines if line.startswith("tolerated")])
        self.assertFalse(tool.tolerated(""))
        self.assertFalse(tool.tolerated("a warning with no ERROR line"))

    def test_status_reads_as_the_caller_and_the_matrix_as_the_launcher(self):
        seen = []

        def runner(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "")

        tool.main(["status", "--kubectl", "kc"], runner=runner, emit=lambda line: None, sleep=lambda seconds: None)
        self.assertTrue(seen and all("--as" not in argv for argv in seen), seen[:1])  # the launcher identity may not read a policy
        seen.clear()
        tool.main(["run", "--kubectl", "kc"], runner=runner, emit=lambda line: None, sleep=lambda seconds: None)
        self.assertTrue(seen and all("--as" in argv for argv in seen), seen[:1])

    def test_the_probe_pod_uses_only_violations_the_api_servers_own_validation_lets_through(self):
        probe = tool.probe_pod(tc.load(tc.GOLDEN / "pod_standard_build.json"))
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertNotIn(field, probe["spec"])  # with hostUsers false the server rejects these itself, before any policy
        self.assertEqual(probe["spec"]["containers"][0]["image"], "registry.example.invalid/factory:latest")

    def test_a_policy_whose_status_is_never_observed_fails_after_a_bounded_wait(self):
        problems, lines, pauses = self.check(observed=False)
        self.assertEqual(problems, 2)
        self.assertEqual(pauses, [tool.PAUSE_SECONDS] * (2 * tool.ATTEMPTS))
        self.assertTrue(all(line.startswith("FAIL policy ") for line in lines))

    def test_the_fixtures_give_the_launcher_only_create_on_pods_and_secrets_and_the_parameter_of_the_binding(self):
        image = f"registry.example.invalid/factory@sha256:{'0' * 64}"
        listing = tool.fixtures(image)
        items = {(item["kind"], item["metadata"]["name"]): item for item in listing["items"]}
        binding = tc.load(tc.POLICY_DIR / "pod-binding.json")["spec"]["paramRef"]
        config = items[("ConfigMap", binding["name"])]
        self.assertEqual((config["metadata"]["namespace"], config["data"]), (binding["namespace"], {"image": image}))
        namespace = next(item for item in listing["items"] if item["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["labels"], {"pod-security.kubernetes.io/enforce": "privileged"})
        self.assertIn(("ServiceAccount", "factory-worker"), items)
        role = items[("Role", "matrix-launcher")]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods", "secrets"], "verbs": ["create"]}])
        subject = items[("RoleBinding", "matrix-launcher")]["subjects"]
        self.assertEqual(subject, [{"kind": "ServiceAccount", "name": "factory", "namespace": "factory"}])

    def test_main_prints_the_fixtures_and_refuses_a_bad_timeout(self):
        lines = []
        self.assertEqual(tool.main(["fixtures"], emit=lines.append), 0)
        self.assertEqual(json.loads(lines[0])["kind"], "List")
        self.assertEqual(tool.main(["run", "--timeout", "0"], emit=lines.append), 2)
        with self.assertRaises(SystemExit) as caught:
            tool.main(["unknown-command"])
        self.assertEqual(caught.exception.code, 2)

    def test_main_returns_one_when_the_matrix_finds_a_problem_and_zero_when_it_passes(self):
        lines = []
        self.assertEqual(tool.main(["run", "--kubectl", "kc"], runner=FakeCluster(), emit=lines.append, sleep=lambda seconds: None), 0)
        pod_policy, _ = tc.policies()
        broken = FakeCluster(pod_policy=tc.without(pod_policy, "host-pid"))
        self.assertEqual(tool.main(["run"], runner=broken, emit=lines.append, sleep=lambda seconds: None), 1)

    def test_main_runs_the_status_command(self):
        cluster = FakeCluster()
        cluster.status = self.status()
        self.assertEqual(tool.main(["status"], runner=cluster, emit=lambda line: None, sleep=lambda seconds: None), 0)


class SourceDiscipline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TOOL_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_it_imports_only_the_standard_library_and_the_pod_contract_and_mirror(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module.split(".")[0])
        self.assertTrue(found <= {*__import__("sys").stdlib_module_names, "factory"}, found)
        self.assertNotIn("random", found)
        self.assertNotIn("os", found, "it reads no environment of its own")

    def test_it_never_uses_a_shell_and_bounds_the_one_process_call_with_a_timeout(self):
        self.assertNotIn("shell=True", self.source)
        self.assertNotIn("os.system", self.source)
        self.assertNotIn("Popen", self.source)
        calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "runner"]
        self.assertEqual(len(calls), 1)
        self.assertIn("timeout", {kw.arg for kw in calls[0].keywords})

    def test_it_builds_no_string_by_concatenation(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                for side in (node.left, node.right):
                    is_text = isinstance(side, ast.JoinedStr) or (isinstance(side, ast.Constant) and isinstance(side.value, str))
                    self.assertFalse(is_text, ast.dump(node)[:80])

    def test_it_prints_through_one_injected_function_and_has_no_print_of_its_own_data(self):
        prints = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"]
        self.assertEqual(prints, [], "output goes through `emit` (print is only its default)")

    def test_the_documented_exit_codes_and_the_limit_are_in_the_header(self):
        for text in ("Exit status: 0", "1 when it found a problem", "2 for a usage", "MAX_NOT_EXERCISED_PERCENT"):
            self.assertIn(text, self.source)


if __name__ == "__main__":
    unittest.main()
