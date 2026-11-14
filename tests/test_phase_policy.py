"""Policy table, generated settings baseline, managed settings and schemas."""

import copy
import factory
import json
import unittest
from pathlib import Path

from factory.model import validate_plan
from factory.phases import (
    ALWAYS_DISALLOWED,
    EDIT_TOOLS,
    POLICIES,
    AgentOptions,
    domain_allows,
    domains_for,
    managed_settings,
    normalize_domains,
    phase_name,
    policy_for,
    probe_host,
    settings_for,
)
from factory.preflight import CAPABILITIES
from factory.schemas import SCHEMAS, schema_for

from tests import schemacheck
from tests.test_factory import assignment

ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def test_every_phase_has_a_policy_and_a_schema(self):
        self.assertEqual(set(POLICIES), {"plan", "build", "review", "verify", "preflight"})
        self.assertEqual(set(SCHEMAS), set(POLICIES))

    def test_web_and_subagent_tools_are_denied_everywhere(self):
        for policy in POLICIES.values():
            self.assertEqual(policy.disallowed[:3], ALWAYS_DISALLOWED, policy.name)
            self.assertEqual(ALWAYS_DISALLOWED, ("WebFetch", "WebSearch", "Task"))

    def test_plan_and_review_are_read_only_build_and_verify_write_the_worktree(self):
        for name in ("plan", "review"):
            policy = POLICIES[name]
            self.assertEqual(policy.write_scope, "none")
            self.assertTrue(set(EDIT_TOOLS) <= set(policy.disallowed))
            self.assertFalse(set(EDIT_TOOLS) & set(policy.tools))
        for name in ("build", "verify"):
            self.assertEqual(POLICIES[name].write_scope, "cwd")
            self.assertTrue({"Edit", "Write", "Bash"} <= set(POLICIES[name].tools))

    def test_network_is_off_except_verify(self):
        options = AgentOptions(verify_domains=("registry.npmjs.org",))
        for name in ("plan", "build", "review"):
            self.assertEqual(domains_for(POLICIES[name], options), ())
        self.assertEqual(domains_for(POLICIES["verify"], options), ("registry.npmjs.org",))
        self.assertEqual(domains_for(POLICIES["verify"], AgentOptions()), ())
        self.assertEqual(domains_for(POLICIES["preflight"], options, "build"), ())
        self.assertEqual(domains_for(POLICIES["preflight"], options, "verify"), ("registry.npmjs.org",))

    def test_models_efforts_and_budgets_match_the_plan(self):
        table = {
            "plan": ("claude-opus-5-5", "high", 5.0),
            "build": ("claude-sonnet-5-5", "high", 8.0),
            "review": ("claude-opus-5-5", "high", 4.0),
            "verify": ("claude-sonnet-5-5", "medium", 6.0),
            "preflight": ("claude-haiku-5-5", "low", 0.5),
        }
        for name, (model, effort, budget) in table.items():
            policy = POLICIES[name]
            self.assertEqual((policy.model, policy.effort, policy.budget_usd), (model, effort, budget))

    def test_attempt_suffixes_resolve_and_unknown_phases_are_rejected(self):
        self.assertEqual(phase_name("build-0"), "build")
        self.assertEqual(phase_name("review-2"), "review")
        self.assertEqual(phase_name("verify"), "verify")
        for bad in ("deploy", "", "publish"):
            with self.assertRaises(ValueError):
                phase_name(bad)

    def test_overrides_apply_per_phase_only(self):
        options = AgentOptions(budgets={"build": 1.5}, models={"review": "m"})
        self.assertEqual(policy_for("build-3", options).budget_usd, 1.5)
        self.assertEqual(policy_for("review-0", options).model, "m")
        self.assertEqual(policy_for("plan", options), POLICIES["plan"])

    def test_domains_and_probe_hosts(self):
        self.assertEqual(normalize_domains(["B.example.com", "a.example.com", "a.example.com"]), ("a.example.com", "b.example.com"))
        self.assertEqual(normalize_domains(["*.pythonhosted.org", "localhost:8080"]), ("*.pythonhosted.org", "localhost:8080"))
        self.assertTrue(domain_allows(("*.pythonhosted.org",), "files.pythonhosted.org"))
        self.assertFalse(domain_allows(("*.pythonhosted.org",), "pythonhosted.org"))
        self.assertTrue(domain_allows(("registry.npmjs.org:443",), "registry.npmjs.org"))
        self.assertFalse(domain_allows((), "registry.npmjs.org"))
        self.assertEqual(probe_host("https://Registry.NPMJS.org/"), "registry.npmjs.org")
        for bad in ("http://registry.npmjs.org/", "registry.npmjs.org", "https:///x"):
            with self.assertRaises(ValueError):
                probe_host(bad)


class ManagedSettingsTests(unittest.TestCase):
    def test_committed_file_matches_the_generator(self):
        committed = json.loads((ROOT / "managed-settings.json").read_text())
        self.assertEqual(committed, managed_settings())

    def test_baseline_locks_the_sandbox_on_and_forbids_bypass(self):
        managed = managed_settings()
        sandbox = managed["sandbox"]
        self.assertIs(sandbox["enabled"], True)
        self.assertIs(sandbox["failIfUnavailable"], True)
        self.assertIs(sandbox["allowUnsandboxedCommands"], False)
        self.assertIs(sandbox["enableWeakerNestedSandbox"], False)
        self.assertIs(sandbox["enableWeakerNetworkIsolation"], False)
        self.assertIs(sandbox["network"]["strictAllowlist"], True)
        self.assertIs(sandbox["network"]["allowAllUnixSockets"], False)
        self.assertEqual(managed["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertEqual(managed["permissions"]["disableAutoMode"], "disable")
        deny = managed["permissions"]["deny"]
        for rule in ("WebFetch", "WebSearch", "Task", "Edit(.claude/**)", "Edit(.mcp.json)", "Edit(.git/**)", "Read(//proc/**)"):
            self.assertIn(rule, deny)

    def test_generated_settings_never_loosen_the_managed_baseline(self):
        managed = managed_settings()
        for name, policy in POLICIES.items():
            settings = settings_for(policy, "/work/tree", ("registry.npmjs.org",))
            for key in ("enabled", "failIfUnavailable", "allowUnsandboxedCommands"):
                self.assertEqual(settings["sandbox"][key], managed["sandbox"][key], (name, key))
            self.assertIs(settings["sandbox"]["network"]["strictAllowlist"], True)
            self.assertTrue(set(settings["permissions"]["deny"]) <= set(managed["permissions"]["deny"]), name)


class SchemaTests(unittest.TestCase):
    def test_schemas_are_json_serialisable_objects_with_consistent_required_keys(self):
        for name, schema in SCHEMAS.items():
            self.assertEqual(schema["type"], "object", name)
            json.dumps(schema)
            self.assertTrue(set(schema["required"]) <= set(schema["properties"]), name)
        self.assertIs(schema_for("build-0"), SCHEMAS["build"])

    def test_planner_schema_accepts_what_validate_plan_accepts(self):
        plan = assignment()
        validate_plan(plan)
        # The supervisor adds `issues` from its own snapshot; the planner does not return them.
        output = {k: v for k, v in plan.items() if k != "issues"}
        self.assertEqual(schemacheck.errors(output, SCHEMAS["plan"]), [])
        for mutate in (
            lambda o: o.update(version=2),
            lambda o: o["jobs"][0].update(id="Bad ID"),
            lambda o: o["jobs"][0].update(feature="false"),
            lambda o: o["jobs"][0].update(likely_paths=[]),
            lambda o: o["dispositions"][0].update(status="done"),
            lambda o: o["dispositions"][0].pop("reason"),
        ):
            broken = copy.deepcopy(output)
            mutate(broken)
            self.assertNotEqual(schemacheck.errors(broken, SCHEMAS["plan"]), [])

    def test_planner_schema_and_prompt_example_cover_tagged_criteria(self):
        prompt = (Path(factory.__file__).parent / "prompts" / "planner.md").read_text()
        example = json.loads(
            next(line for line in prompt.splitlines() if line.startswith('{"version":1'))
        )
        self.assertEqual(schemacheck.errors(example, SCHEMAS["plan"]), [])
        for tag in ("sandbox", "ci", "operator", "never whether"):
            self.assertIn(tag, prompt)
        tags = {c["tag"] for c in example["jobs"][0]["acceptance"]}
        self.assertEqual(tags, {"sandbox", "ci"})
        # the example, with the supervisor's issue snapshot, is a valid plan
        validate_plan({**example, "issues": [{"number": 123, "title": "t", "body": "b"}]})
        # a planner result mixing plain and tagged criteria
        plan = assignment()
        plan["jobs"][0]["acceptance"] = [
            "plain",
            {"text": "scan", "tag": "ci", "checks": ["trivy"]},
            {"text": "prod", "tag": "operator"},
        ]
        validate_plan(plan)
        output = {k: v for k, v in plan.items() if k != "issues"}
        self.assertEqual(schemacheck.errors(output, SCHEMAS["plan"]), [])
        # an operator criterion may say why only a person can prove it
        plan["jobs"][0]["acceptance"].append({"text": "prod login", "tag": "operator", "reason": "needs production"})
        validate_plan(plan)
        output = {k: v for k, v in plan.items() if k != "issues"}
        self.assertEqual(schemacheck.errors(output, SCHEMAS["plan"]), [])
        output["jobs"][0]["acceptance"].pop()
        for bad in (
            {"text": "x", "tag": "operator", "reason": ""},
            {"text": "x", "tag": "operator", "reason": 7},
            {"text": "x", "tag": "operator", "note": "y"},
            {"text": "x", "tag": "prod"},
            {"tag": "ci"},
            {"text": "x", "run": "y"},
            3,
        ):
            broken = copy.deepcopy(output)
            broken["jobs"][0]["acceptance"] = [bad]
            self.assertNotEqual(schemacheck.errors(broken, SCHEMAS["plan"]), [], bad)

    def test_verify_prompt_says_where_never_whether(self):
        prompt = (Path(factory.__file__).parent / "prompts" / "verify.md").read_text()
        self.assertIn("never whether", prompt)
        self.assertIn("never skip it silently", prompt)
        self.assertIn("unproven, never satisfied", prompt)

    def test_prompts_keep_operator_criteria_out_of_what_complete_covers(self):
        prompts = Path(factory.__file__).parent / "prompts"
        verify = (prompts / "verify.md").read_text()
        self.assertIn("listed apart as an operator-only item", verify)
        self.assertIn("never set complete=false or add a blocker because of it", verify)
        self.assertNotIn("unproven ci or operator", verify)
        self.assertIn("never fail the review because one is unproven", (prompts / "reviewer.md").read_text())
        planner = (prompts / "planner.md").read_text()
        self.assertIn('"tag":"operator","reason"', planner)
        self.assertIn("never use `operator` for anything the sandbox or CI can prove", planner)

    def test_builder_schema_matches_the_runner_validator(self):
        good = {"summary": "s", "blocked": False, "blocker": "", "tests": ["t"], "feature": False}
        self.assertEqual(schemacheck.errors(good, SCHEMAS["build"]), [])
        self.assertEqual(schemacheck.errors({k: v for k, v in good.items() if k != "blocker"}, SCHEMAS["build"]), [])
        for key, value in (("feature", None), ("blocked", "no"), ("tests", "t"), ("tests", [1]), ("summary", 3)):
            broken = dict(good, **{key: value})
            self.assertNotEqual(schemacheck.errors(broken, SCHEMAS["build"]), [], (key, value))

    def test_reviewer_and_verify_schemas(self):
        self.assertEqual(schemacheck.errors({"pass": True, "findings": []}, SCHEMAS["review"]), [])
        self.assertNotEqual(schemacheck.errors({"pass": "yes", "findings": []}, SCHEMAS["review"]), [])
        self.assertNotEqual(schemacheck.errors({"pass": True}, SCHEMAS["review"]), [])
        verify = {"complete": False, "scenario": "s", "commit": "c", "environment": "e", "recording_path": "", "recording_url": "", "assertions": ["a"], "blocker": "b"}
        self.assertEqual(schemacheck.errors(verify, SCHEMAS["verify"]), [])
        self.assertNotEqual(schemacheck.errors({"complete": "no"}, SCHEMAS["verify"]), [])

    def test_preflight_schema_requires_exactly_the_probe_capabilities(self):
        self.assertEqual(set(SCHEMAS["preflight"]["required"]), CAPABILITIES)
        good = {name: {"status": "available", "evidence": "ok"} for name in CAPABILITIES}
        self.assertEqual(schemacheck.errors(good, SCHEMAS["preflight"]), [])
        missing = dict(good)
        missing.pop("network_download")
        self.assertNotEqual(schemacheck.errors(missing, SCHEMAS["preflight"]), [])
        bad = dict(good, workspace_write={"status": "maybe", "evidence": "x"})
        self.assertNotEqual(schemacheck.errors(bad, SCHEMAS["preflight"]), [])
        empty = dict(good, workspace_write={"status": "available", "evidence": ""})
        self.assertNotEqual(schemacheck.errors(empty, SCHEMAS["preflight"]), [])


if __name__ == "__main__":
    unittest.main()
