"""Choosing the model and effort: repository defaults and per-issue labels (src/factory/model_choice.py).

Pure rules first (precedence, validation, grouping), then the places they are used: the planner
(a refused label blocks the issue before any session), the append fragment, the worker invocation,
the status report and the pull request body. A fake ``gh`` and a fake planner session stand in for
GitHub and Claude; nothing here touches a network.
"""

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import cli, fragments, model_choice, planner, trigger
from factory.config import RepoConfig
from factory.issue_comments import marker
from factory.model import validate_plan
from factory.model_choice import (
    DEFAULT_ALIASES,
    EMPTY,
    Directive,
    assign,
    blocked_reason,
    check_choice,
    choice_labels,
    issue_directive,
    job_choice,
    phase_choice,
    phase_override,
    screen,
)
from factory.phases import EFFORTS, POLICIES, AgentOptions, policy_for
from factory.runner import Runner, models_block
from factory.status import format_status, status_report
from factory.trigger import IssueFacts, classify

from tests.test_agent import DUMP_ARGV, AgentTestCase
from tests.test_factory import assignment
from tests.test_issue_comments import FakeGhCase as CommentGhCase
from tests.test_issue_comments import plan_with
from tests.test_epic import REASON as EPIC_REASON
from tests.test_epic import disposition as epic_disposition
from tests.test_plan_append import AppendCase
from tests.test_plan_issues import SECRET, FakeGh, entry

OPUS, SONNET, HAIKU = (DEFAULT_ALIASES[name] for name in ("opus", "sonnet", "haiku"))


def issue(*labels, body=""):
    return {"number": 1, "title": "T", "body": body, "labels": [{"name": name} for name in labels]}


def directive(*labels, options=None):
    value, codes = issue_directive(issue(*labels), options or AgentOptions())
    return value if not codes else codes


class PrecedenceTests(unittest.TestCase):
    """Most specific wins: issue, then [models]/[efforts], then the repository default, then built-in."""

    LAYERS = {
        "built-in": AgentOptions(),
        "repository": AgentOptions(default_model="m-default", default_effort="low"),
        "repository-phase": AgentOptions(
            default_model="m-default",
            default_effort="low",
            models={"build": "m-phase", "verify": "m-phase", "review": "m-phase"},
            efforts={"build": "medium", "verify": "medium", "review": "medium"},
        ),
    }

    def test_the_matrix_for_the_phases_an_issue_chooses_for(self):
        expected = {
            "built-in": {"build": (SONNET, "high"), "verify": (SONNET, "medium")},
            "repository": {"build": ("m-default", "low"), "verify": ("m-default", "low")},
            "repository-phase": {"build": ("m-phase", "medium"), "verify": ("m-phase", "medium")},
        }
        for layer, options in self.LAYERS.items():
            for phase in ("build", "verify"):
                with self.subTest(layer=layer, phase=phase):
                    got = phase_choice(phase, options)
                    self.assertEqual((got["model"], got["effort"]), expected[layer][phase])
                    self.assertEqual((got["model_source"], got["effort_source"]), (layer, layer))
                    # The recorded value is what policy_for resolves without an issue.
                    policy = policy_for(phase, options)
                    self.assertEqual((policy.model, policy.effort), expected[layer][phase])
                with self.subTest(layer=f"issue over {layer}", phase=phase):
                    got = phase_choice(phase, options, Directive("m-issue", "max"))
                    self.assertEqual(
                        got,
                        {"model": "m-issue", "effort": "max", "model_source": "issue", "effort_source": "issue"},
                    )
                    policy = policy_for(phase, options, {"model": "m-issue", "effort": "max"})
                    self.assertEqual((policy.model, policy.effort), ("m-issue", "max"))

    def test_model_and_effort_are_resolved_independently(self):
        options = self.LAYERS["repository-phase"]
        only_model = phase_choice("build", options, Directive(model="m-issue"))
        self.assertEqual(
            (only_model["model"], only_model["model_source"], only_model["effort"], only_model["effort_source"]),
            ("m-issue", "issue", "medium", "repository-phase"),
        )
        only_effort = phase_choice("verify", self.LAYERS["repository"], Directive(effort="xhigh"))
        self.assertEqual(
            (only_effort["model"], only_effort["model_source"], only_effort["effort"], only_effort["effort_source"]),
            ("m-default", "repository", "xhigh", "issue"),
        )

    def test_an_issue_never_changes_the_reviewer_or_the_planner(self):
        options = self.LAYERS["repository"]
        for phase in ("review", "plan"):
            got = phase_choice(phase, options, Directive("m-issue", "max"))
            self.assertEqual((got["model"], got["effort"]), ("m-default", "low"), phase)
            self.assertEqual((got["model_source"], got["effort_source"]), ("repository",) * 2)
        self.assertEqual(set(job_choice(options, Directive("m-issue", "max"))), {"build", "verify"})
        self.assertEqual(policy_for("review", options).model, "m-default")

    def test_the_repository_default_skips_the_preflight_probe(self):
        options = self.LAYERS["repository"]
        probe = policy_for("preflight", options)
        self.assertEqual((probe.model, probe.effort), (HAIKU, "low"))
        for phase in ("plan", "build", "review", "verify"):
            self.assertEqual(policy_for(phase, options).model, "m-default", phase)
        # ...but the per-phase tables still reach it.
        both = AgentOptions(default_model="m-default", models={"preflight": "m-probe"}, efforts={"preflight": "medium"})
        self.assertEqual(
            (policy_for("preflight", both).model, policy_for("preflight", both).effort), ("m-probe", "medium")
        )
        self.assertEqual(phase_choice("preflight", options)["model_source"], "built-in")

    def test_without_any_new_option_every_phase_is_the_built_in_policy(self):
        for phase, policy in POLICIES.items():
            self.assertEqual(policy_for(phase), policy)
            self.assertEqual(policy_for(phase, AgentOptions(models={}, efforts={})), policy)
        only_models = AgentOptions(models={"build": "m-1"}, efforts={"verify": "low"})
        self.assertEqual((policy_for("build", only_models).model, policy_for("build", only_models).effort), ("m-1", "high"))
        self.assertEqual(policy_for("verify", only_models).effort, "low")

    def test_the_policy_rejects_a_malformed_choice(self):
        for choice in ({"model": " "}, {"model": 5}, {"effort": "extreme"}):
            with self.subTest(choice), self.assertRaises(ValueError):
                policy_for("build", AgentOptions(), choice)
        with self.assertRaises(ValueError):
            policy_for("build", AgentOptions(default_effort="extreme"))
        with self.assertRaises(ValueError):
            policy_for("build", AgentOptions(default_model=""))

    def test_the_budget_never_depends_on_the_model_choice(self):
        options = AgentOptions(budgets={"build": 2.25})
        self.assertEqual(policy_for("build", options, {"model": OPUS, "effort": "max"}).budget_usd, 2.25)
        self.assertEqual(policy_for("verify", AgentOptions(), {"model": OPUS}).budget_usd, POLICIES["verify"].budget_usd)


class AllowedModelsAndCeilingTests(unittest.TestCase):
    def test_the_default_allowed_models_are_the_built_in_ids_plus_the_repository_s_own(self):
        self.assertEqual(model_choice.allowed_models(AgentOptions()), {OPUS, SONNET, HAIKU})
        options = AgentOptions(
            default_model="m-default", models={"build": "m-phase"}, aliases={"fast": "m-alias"}
        )
        self.assertEqual(
            model_choice.allowed_models(options), {OPUS, SONNET, HAIKU, "m-default", "m-phase", "m-alias"}
        )

    def test_an_explicit_allowed_models_is_exact(self):
        options = AgentOptions(allowed_models=(SONNET,), default_model="m-default", models={"build": "m-phase"})
        self.assertEqual(model_choice.allowed_models(options), {SONNET})
        self.assertEqual(directive("factory-model:sonnet", options=options), Directive(SONNET))
        # Neither a built-in alias target nor the repository's own default id is added.
        self.assertEqual(directive("factory-model:opus", options=options), ("model-not-allowed",))
        self.assertEqual(directive("factory-model:m-default", options=options), ("model-not-allowed",))
        self.assertEqual(model_choice.allowed_models(AgentOptions(allowed_models=())), frozenset())
        self.assertEqual(directive("factory-model:sonnet", options=AgentOptions(allowed_models=())), ("model-not-allowed",))

    def test_a_repository_that_sets_model_can_select_it_from_an_issue(self):
        options = AgentOptions(default_model="vendor.model-x")
        self.assertEqual(directive("factory-model:vendor.model-x", options=options), Directive("vendor.model-x"))

    def test_the_default_ceiling_is_the_highest_effort_in_use(self):
        self.assertEqual(model_choice.effort_ceiling(AgentOptions()), "high")
        self.assertEqual(model_choice.effort_ceiling(AgentOptions(default_effort="low")), "high")
        self.assertEqual(model_choice.effort_ceiling(AgentOptions(default_effort="xhigh")), "xhigh")
        self.assertEqual(model_choice.effort_ceiling(AgentOptions(efforts={"plan": "max"})), "max")
        # An explicit max_effort is exact, in either direction.
        self.assertEqual(model_choice.effort_ceiling(AgentOptions(max_effort="low", default_effort="max")), "low")
        self.assertEqual(model_choice.effort_ceiling(AgentOptions(max_effort="max")), "max")


class IssueDirectiveTests(unittest.TestCase):
    def test_no_label_is_an_empty_directive(self):
        for labels in ((), ("bug", "factory-build", "factory:planned", "factory:blocked")):
            self.assertEqual(directive(*labels), EMPTY)
            self.assertTrue(directive(*labels).empty)

    def test_an_alias_becomes_its_full_id_and_matches_case_insensitively(self):
        for label, model in (
            ("factory-model:opus", OPUS),
            ("factory-model:Sonnet", SONNET),
            ("FACTORY-MODEL:HAIKU", HAIKU),
            ("Factory-Model: opus ", OPUS),
            (f"factory-model:{OPUS}", OPUS),
        ):
            with self.subTest(label):
                self.assertEqual(directive(label), Directive(model=model))

    def test_efforts_match_case_insensitively_up_to_the_ceiling(self):
        for level in ("low", "MEDIUM", "High"):
            self.assertEqual(directive(f"factory-effort:{level}"), Directive(effort=level.lower()))
        self.assertEqual(directive("factory-effort:xhigh"), ("effort-above-max",))
        self.assertEqual(directive("factory-effort:max"), ("effort-above-max",))
        raised = AgentOptions(max_effort="max")
        self.assertEqual(directive("factory-effort:max", options=raised), Directive(effort="max"))
        self.assertEqual(directive("factory-effort:xhigh", options=AgentOptions(max_effort="medium")), ("effort-above-max",))
        self.assertEqual(directive("factory-effort:medium", options=AgentOptions(max_effort="medium")), Directive(effort="medium"))

    def test_both_labels_make_one_directive(self):
        self.assertEqual(
            directive("factory-effort:low", "factory-model:haiku", "bug"), Directive(HAIKU, "low")
        )

    def test_the_repository_s_aliases_override_and_extend_the_built_in_ones(self):
        options = AgentOptions(aliases={"opus": "m-new-opus", "fast": HAIKU})
        self.assertEqual(directive("factory-model:Opus", options=options), Directive("m-new-opus"))
        self.assertEqual(directive("factory-model:fast", options=options), Directive(HAIKU))
        self.assertEqual(directive("factory-model:sonnet", options=options), Directive(SONNET))
        table = model_choice.alias_table(options)
        self.assertEqual((table["opus"], table["sonnet"], table["fast"]), ("m-new-opus", SONNET, HAIKU))
        # The built-in table itself is never changed by a repository.
        self.assertEqual(DEFAULT_ALIASES["opus"], OPUS)

    def test_an_alias_target_outside_allowed_models_is_refused(self):
        options = AgentOptions(allowed_models=(SONNET,), aliases={"fast": "m-fast"})
        self.assertEqual(directive("factory-model:fast", options=options), ("model-not-allowed",))
        self.assertEqual(directive("factory-model:fast", options=AgentOptions(aliases={"fast": "m-fast"})), Directive("m-fast"))

    def test_unknown_and_malformed_values_are_refused(self):
        self.assertEqual(directive("factory-model:gpt"), ("model-not-allowed",))
        self.assertEqual(directive("factory-model:claude-unlisted-9"), ("model-not-allowed",))
        self.assertEqual(directive("factory-model:not a model!"), ("unknown-model",))
        self.assertEqual(directive("factory-model:"), ("unknown-model",))
        self.assertEqual(directive("factory-effort:extreme"), ("unknown-effort",))
        self.assertEqual(directive("factory-effort:"), ("unknown-effort",))

    def test_no_prefix_or_fuzzy_matching(self):
        for value in ("op", "opu", "opus-5-5", "claude-opus", "claude-opus-5", "opuss", "sonnet4", "claude-haiku-5-"):
            with self.subTest(value):
                self.assertEqual(directive(f"factory-model:{value}"), ("model-not-allowed",))
        for value in ("hig", "highest", "med", "x-high", "very high"):
            with self.subTest(value):
                self.assertEqual(directive(f"factory-effort:{value}"), ("unknown-effort",))

    def test_two_labels_of_one_family_are_refused_never_resolved(self):
        self.assertEqual(directive("factory-model:opus", "factory-model:sonnet"), ("duplicate-model-label",))
        self.assertEqual(directive("factory-effort:low", "factory-effort:high"), ("duplicate-effort-label",))
        self.assertEqual(
            directive("factory-model:opus", "factory-model:sonnet", "factory-effort:low", "factory-effort:high"),
            ("duplicate-model-label", "duplicate-effort-label"),
        )
        self.assertEqual(
            directive("factory-model:gpt", "factory-effort:max"), ("model-not-allowed", "effort-above-max")
        )

    def test_only_the_two_label_families_are_read(self):
        self.assertEqual(directive("factory:model=opus", "factory-modelopus", "model:opus", "effort:max", "factory_model:opus"), EMPTY)
        # Factory's own status labels live in the `factory:` namespace and are never a choice.
        self.assertEqual(directive("factory:planned", "factory:building", "factory:ready"), EMPTY)
        # Plain strings (older snapshots) and odd entries are handled.
        value, codes = issue_directive({"labels": ["factory-model:opus", 5, None, {"name": None}]}, AgentOptions())
        self.assertEqual((value, codes), (Directive(model=OPUS), ()))
        self.assertEqual(issue_directive({"labels": None}, AgentOptions()), (EMPTY, ()))
        self.assertEqual(issue_directive({}, AgentOptions()), (EMPTY, ()))

    def test_text_in_the_issue_body_or_title_never_selects_anything(self):
        text = "factory: model=opus effort=max\nfactory-model:opus\nfactory-effort:max"
        value, codes = issue_directive({"number": 1, "title": text, "body": text, "comments": [{"body": text}], "labels": []}, AgentOptions())
        self.assertEqual((value, codes), (EMPTY, ()))

    def test_refusal_reasons_are_fixed_text_that_never_carries_the_label(self):
        weird = "factory-model:SECRET-LABEL-TEXT-do-not-print"
        codes = directive(weird, "factory-effort:SECRET-EFFORT-do-not-print")
        self.assertEqual(codes, ("model-not-allowed", "unknown-effort"))
        reason = blocked_reason(codes)
        self.assertNotIn("SECRET", reason)
        self.assertIn("allowed_models", reason)
        self.assertIn("not one of low, medium, high, xhigh, max", reason)

    def test_the_reason_fits_a_fragment_disposition_even_with_every_code(self):
        longest = blocked_reason(
            ("duplicate-model-label", "duplicate-effort-label", "model-not-allowed", "unknown-effort", "effort-above-max")
        )
        self.assertLessEqual(len(longest), fragments.MAX_REASON)
        for code in model_choice.REASONS:
            self.assertLessEqual(len(blocked_reason((code,))), fragments.MAX_REASON, code)
        self.assertEqual(model_choice.blocked(7, ("unknown-model",))["status"], "blocked")


class ScreenAuthorityTests(unittest.TestCase):
    """Who applied a choice label decides before what it says (applying a label needs only triage access)."""

    def screen(self, issues, authority=None, options=None):
        return screen(issues, options or AgentOptions(), authority)

    def numbered(self, number, *labels):
        return {**issue(*labels), "number": number}

    def test_the_choice_labels_are_the_two_families_as_written(self):
        names = ("bug", "Factory-Model:Opus", "factory:planned", "factory-effort: high", "factory-build", "model:opus")
        self.assertEqual(choice_labels(issue(*names)), ["Factory-Model:Opus", "factory-effort: high"])
        self.assertEqual(choice_labels({"labels": ["factory-model:opus", 5, None]}), ["factory-model:opus"])
        self.assertEqual((choice_labels(issue()), choice_labels({}), choice_labels({"labels": None})), ([], [], []))

    def test_an_issue_without_a_choice_label_never_asks_and_needs_no_authority(self):
        asked = []
        issues = [self.numbered(1), self.numbered(2, "bug", "factory-build", "factory:planned")]
        directives, refused = self.screen(issues, lambda number, labels: asked.append(number))
        self.assertEqual((directives, refused, asked), ({1: EMPTY, 2: EMPTY}, {}, []))
        self.assertEqual(self.screen(issues)[1], {})

    def test_a_choice_label_without_an_authority_is_refused_never_honoured(self):
        directives, refused = self.screen([self.numbered(1, "factory-model:opus", "factory-effort:high")])
        self.assertEqual((directives, refused), ({}, {1: ("label-not-authorised",)}))

    def test_the_authority_is_asked_once_per_labelled_issue_with_the_choice_labels_only(self):
        asked = []

        def authority(number, labels):
            asked.append((number, labels))
            return True

        issues = [
            self.numbered(1, "bug", "Factory-Model:OPUS", "factory-effort:low", "factory-build"),
            self.numbered(2),
            self.numbered(3, "factory-effort:medium"),
        ]
        directives, refused = self.screen(issues, authority)
        self.assertEqual(asked, [(1, ["Factory-Model:OPUS", "factory-effort:low"]), (3, ["factory-effort:medium"])])
        self.assertEqual(refused, {})
        self.assertEqual(directives, {1: Directive(OPUS, "low"), 2: EMPTY, 3: Directive(effort="medium")})

    def test_a_label_applied_by_someone_who_may_not_choose_is_refused_and_not_replaced_by_a_default(self):
        issues = [self.numbered(1, "factory-model:opus", "factory-effort:high"), self.numbered(2, "factory-model:haiku")]
        directives, refused = self.screen(issues, lambda number, labels: number == 2)
        self.assertEqual(directives, {2: Directive(HAIKU)})
        self.assertEqual(refused, {1: ("label-not-authorised",)})

    def test_who_applied_it_comes_before_what_it_says(self):
        # An unauthorised person's invalid or duplicate labels are reported as the one reason that matters.
        issues = [self.numbered(1, "factory-model:gpt", "factory-effort:max"), self.numbered(2, "factory-model:opus", "factory-model:haiku")]
        _, refused = self.screen(issues, lambda number, labels: False)
        self.assertEqual(refused, {1: ("label-not-authorised",), 2: ("label-not-authorised",)})
        # Once they are authorised the values are validated as before.
        _, refused = self.screen(issues, lambda number, labels: True)
        self.assertEqual(refused, {1: ("model-not-allowed", "effort-above-max"), 2: ("duplicate-model-label",)})

    def test_the_reason_is_fixed_text_that_never_carries_the_label(self):
        reason = blocked_reason(("label-not-authorised",))
        self.assertLessEqual(len(reason), fragments.MAX_REASON)
        self.assertIn("auto_build_min_permission", reason)
        self.assertIn("factory-model", reason)
        self.assertEqual(model_choice.blocked(3, ("label-not-authorised",))["status"], "blocked")


class TriggerIsUnaffectedTests(unittest.TestCase):
    """The choice labels are not status labels (``factory:``) and never trigger or block a build."""

    def facts(self, *names):
        return IssueFacts(1, tuple(names))

    def test_choice_labels_do_not_change_how_the_trigger_reads_an_issue(self):
        choice = ("factory-model:opus", "factory-effort:low")
        self.assertEqual(classify(self.facts("factory-build", *choice), "factory-build"), "eligible")
        self.assertEqual(classify(self.facts("factory-build", "factory:blocked", *choice), "factory-build"), "retry")
        self.assertEqual(classify(self.facts("factory-build", "factory:building", *choice), "factory-build"), "in-flight")
        self.assertEqual(classify(self.facts("factory-build", "factory-ignore", *choice), "factory-build"), "ignored")

    def test_a_choice_label_alone_triggers_nothing(self):
        self.assertIsNone(classify(self.facts("factory-model:opus", "factory-effort:max"), "factory-build"))


class AssignTests(unittest.TestCase):
    def jobs(self):
        plan = assignment()
        job = plan["jobs"][0]
        second = dict(job, id="fix-b", issues=[2], likely_paths=["docs"], depends_on=[])
        third = dict(job, id="fix-c", issues=[3], likely_paths=["lib"], depends_on=["fix-b"])
        dispositions = [
            {"issue": 1, "status": "job", "job": "fix-a", "reason": "ok"},
            {"issue": 2, "status": "job", "job": "fix-b", "reason": "ok"},
            {"issue": 3, "status": "job", "job": "fix-c", "reason": "ok"},
            {"issue": 4, "status": "covered", "reason": "done"},
        ]
        return [job, second, third], dispositions

    def test_every_job_records_a_choice_with_the_source_of_each_value(self):
        jobs, dispositions = self.jobs()
        options = AgentOptions(default_effort="low")
        kept, kept_dispositions = assign(jobs, dispositions, {1: Directive(model=OPUS)}, options)
        self.assertEqual([j["id"] for j in kept], ["fix-a", "fix-b", "fix-c"])
        self.assertEqual(kept_dispositions, dispositions)
        self.assertEqual(kept[0]["choice"]["build"]["model"], OPUS)
        self.assertEqual(kept[0]["choice"]["build"]["model_source"], "issue")
        self.assertEqual(kept[0]["choice"]["verify"]["effort_source"], "repository")
        self.assertEqual(kept[1]["choice"]["build"]["model_source"], "built-in")
        for job in kept:
            check_choice(job["choice"])
        self.assertNotIn("choice", jobs[0], "the planner's own dicts are not touched")

    def test_a_choice_written_by_the_planner_is_dropped(self):
        jobs, dispositions = self.jobs()
        jobs[0]["choice"] = {"build": {"model": "planner-picked"}}
        kept, _ = assign(jobs, dispositions, {}, AgentOptions())
        self.assertEqual(kept[0]["choice"]["build"]["model"], SONNET)

    def test_a_job_whose_issues_disagree_is_blocked_with_its_dependents(self):
        jobs, dispositions = self.jobs()
        jobs[0]["issues"] = [1, 5]
        dispositions.append({"issue": 5, "status": "job", "job": "fix-a", "reason": "ok"})
        jobs[1]["depends_on"] = ["fix-a"]  # fix-b waits for the conflicting job, and fix-c for fix-b
        kept, kept_dispositions = assign(jobs, dispositions, {1: Directive(model=OPUS), 5: Directive(model=HAIKU)}, AgentOptions())
        self.assertEqual(kept, [])
        by_issue = {d["issue"]: d for d in kept_dispositions}
        for number in (1, 5, 2, 3):
            self.assertEqual(by_issue[number]["status"], "blocked", number)
            self.assertNotIn("job", by_issue[number])
        self.assertIn("different factory-model or factory-effort labels", by_issue[1]["reason"])
        self.assertIn("depends on was blocked", by_issue[2]["reason"])
        self.assertIn("depends on was blocked", by_issue[3]["reason"])
        self.assertEqual(by_issue[4]["status"], "covered")

    def test_an_unlabelled_issue_differs_from_a_labelled_one_but_equal_directives_group(self):
        jobs, dispositions = self.jobs()
        jobs[0]["issues"] = [1, 5]
        dispositions.append({"issue": 5, "status": "job", "job": "fix-a", "reason": "ok"})
        kept, _ = assign(jobs, dispositions, {1: Directive(model=OPUS)}, AgentOptions())
        self.assertEqual([j["id"] for j in kept], ["fix-b", "fix-c"], "fix-a mixes a labelled and an unlabelled issue")
        kept, _ = assign(copy.deepcopy(jobs), dispositions, {1: Directive(model=OPUS), 5: Directive(model=OPUS)}, AgentOptions())
        self.assertEqual(kept[0]["id"], "fix-a")
        self.assertEqual(kept[0]["choice"]["build"]["model"], OPUS)

    def test_only_the_dependents_of_a_blocked_job_are_blocked(self):
        jobs, dispositions = self.jobs()
        jobs[0]["issues"] = [1, 5]
        dispositions.append({"issue": 5, "status": "job", "job": "fix-a", "reason": "ok"})
        kept, kept_dispositions = assign(jobs, dispositions, {1: Directive(model=OPUS)}, AgentOptions())
        self.assertEqual({d["issue"]: d["status"] for d in kept_dispositions}, {1: "blocked", 5: "blocked", 2: "job", 3: "job", 4: "covered"})


class RecordedChoiceTests(unittest.TestCase):
    def choice(self):
        return job_choice(AgentOptions(), Directive(OPUS, "low"))

    def test_a_well_formed_choice_validates_in_the_plan_and_in_a_fragment(self):
        plan = assignment()
        plan["jobs"][0]["choice"] = self.choice()
        validate_plan(plan)
        job = {k: v for k, v in plan["jobs"][0].items() if k != "fragment"}
        issues, jobs, _ = fragments.parse(
            json.dumps({"version": 1, "kind": "plan_fragment", "issues": [{"number": 2, "title": "t", "body": "b"}], "jobs": [dict(job, id="fix-b", issues=[2])]}).encode(),
            10,
        )
        self.assertEqual(jobs[0]["choice"], self.choice())

    def test_malformed_choices_are_refused_everywhere(self):
        good = self.choice()
        mutations = {
            "not a table": lambda c: "opus",
            "missing phase": lambda c: {"build": c["build"]},
            "extra phase": lambda c: {**c, "review": c["build"]},
            "extra key": lambda c: {**c, "build": {**c["build"], "x": 1}},
            "missing key": lambda c: {**c, "build": {k: v for k, v in c["build"].items() if k != "effort"}},
            "bad model": lambda c: {**c, "build": {**c["build"], "model": "a b"}},
            "model type": lambda c: {**c, "build": {**c["build"], "model": 5}},
            "bad effort": lambda c: {**c, "verify": {**c["verify"], "effort": "extreme"}},
            "bad model source": lambda c: {**c, "verify": {**c["verify"], "model_source": "magic"}},
            "bad effort source": lambda c: {**c, "verify": {**c["verify"], "effort_source": None}},
        }
        for name, mutate in mutations.items():
            bad = mutate(copy.deepcopy(good))
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    check_choice(bad)
                plan = assignment()
                plan["jobs"][0]["choice"] = bad
                with self.assertRaises(ValueError):
                    validate_plan(plan)
                document = {"version": 1, "kind": "plan_fragment", "issues": [{"number": 2, "title": "t", "body": "b"}],
                            "jobs": [dict(assignment()["jobs"][0], id="fix-b", issues=[2], choice=bad)]}
                with self.assertRaises(fragments.FragmentError) as caught:
                    fragments.parse(json.dumps(document).encode(), 10)
                self.assertEqual(caught.exception.reason, "schema:choice")

    def test_phase_override_is_only_for_build_and_verify_and_only_for_a_valid_record(self):
        job = {"choice": self.choice()}
        self.assertEqual(phase_override(job, "build"), {"model": OPUS, "effort": "low"})
        self.assertEqual(phase_override(job, "verify"), {"model": OPUS, "effort": "low"})
        for phase in ("review", "plan", "preflight"):
            self.assertIsNone(phase_override(job, phase), phase)
        self.assertIsNone(phase_override({}, "build"), "a plan made before choices were recorded")
        self.assertIsNone(phase_override({"choice": {"build": {}}}, "build"))
        self.assertIsNone(phase_override(None, "build"))
        self.assertIsNone(model_choice.recorded_choice({}))
        self.assertIsNone(model_choice.recorded_choice({"choice": 3}))

    def test_describe_names_the_source_of_each_value(self):
        entry = self.choice()["build"]
        self.assertEqual(model_choice.describe(entry), f"{OPUS} (issue), effort low (issue)")
        self.assertIn("(issue label)", model_choice.describe(entry, long=True))
        self.assertIn("built-in default", model_choice.describe(phase_choice("build", AgentOptions()), long=True))
        self.assertIn("repository per-phase setting", model_choice.describe(phase_choice("build", AgentOptions(models={"build": "m"})), long=True))

    def test_the_labels_to_create_are_derived_from_the_aliases_and_efforts(self):
        styles = model_choice.label_styles(AgentOptions(aliases={"fast": HAIKU}))
        self.assertEqual(
            sorted(styles),
            sorted([f"factory-model:{a}" for a in ("fast", "haiku", "opus", "sonnet")] + [f"factory-effort:{e}" for e in EFFORTS]),
        )
        for name, (color, description) in styles.items():
            self.assertRegex(color, r"[0-9a-f]{6}")
            self.assertTrue(description.startswith("Factory: "), name)
            self.assertLessEqual(len(name), 50)
            self.assertFalse(name.startswith("factory:"), "never the status namespace")


# ---------------------------------------------------------------------------
# The planner: refused labels block before any session, accepted ones are recorded
# ---------------------------------------------------------------------------


class PlannerCase(FakeGh):
    """``planner.plan`` with a fake ``gh`` and a fake planner session that records its prompts."""

    def setUp(self):
        super().setUp()
        self.prompts, self.paths = [], []
        self.canned = None
        self.asked = []  # (issue number, label names) of every authority question
        self.authority = self.allow

    def allow(self, number, labels):
        """The authority of a repository where every applier may choose (the tests below that are not about who)."""
        self.asked.append((number, list(labels)))
        return True

    def fake_agent(self, prompt, cwd, prefix, phase, *args, **kwargs):
        self.prompts.append(prompt)
        self.paths.append(kwargs.get("read_paths"))
        return copy.deepcopy(self.canned), {"total_cost_usd": 0.5}

    def job_for(self, number, name=None, **extra):
        return {
            "id": name or f"fix-{number}",
            "issues": [number],
            "context_issues": [],
            "objective": f"Fix {number}",
            "likely_paths": [f"src/m{number}"],
            "acceptance": ["it works"],
            "depends_on": [],
            "feature": False,
            **extra,
        }

    def answer(self, *numbers, **jobs):
        self.canned = {
            "version": 1,
            "jobs": [self.job_for(n) for n in numbers],
            "dispositions": [{"issue": n, "status": "job", "job": f"fix-{n}", "reason": "ok"} for n in numbers],
        }

    def run_plan(self, numbers, options=None, name="plan", **kwargs):
        destination = self.root / name
        kwargs.setdefault("authority", self.authority)
        with patch("factory.planner.run_agent", side_effect=self.fake_agent):
            return self.capture(
                planner.plan, self.work, destination, 30, 60, False, options, "org/name", numbers=list(numbers), **kwargs
            )


class PlanTimeResolutionTests(PlannerCase):
    def test_a_labelled_issue_records_its_choice_and_the_unlabelled_one_the_defaults(self):
        self.serve(n1=entry("one", "factory-model:opus", "factory-effort:low"), n2=entry("two"))
        self.answer(1, 2)
        options = AgentOptions(default_model="m-default")
        result = self.run_plan([1, 2], options)
        first, second = result["jobs"]
        self.assertEqual(
            first["choice"]["build"],
            {"model": OPUS, "effort": "low", "model_source": "issue", "effort_source": "issue"},
        )
        self.assertEqual(
            first["choice"]["verify"],
            {"model": OPUS, "effort": "low", "model_source": "issue", "effort_source": "issue"},
        )
        self.assertEqual(
            second["choice"]["build"],
            {"model": "m-default", "effort": "high", "model_source": "repository", "effort_source": "built-in"},
        )
        saved = json.loads((self.root / "plan" / "plan.json").read_text())
        self.assertEqual(saved["jobs"][0]["choice"], first["choice"])
        validate_plan(saved)
        self.assertEqual(self.err, "")

    def test_the_planner_is_shown_the_validated_choice_and_cannot_set_one(self):
        self.serve(n1=entry("one", "factory-model:haiku"), n2=entry("two"))
        self.answer(1, 2)
        self.canned["jobs"][1]["choice"] = {"build": {"model": "planner-picked"}}
        result = self.run_plan([1, 2])
        prompt = self.prompts[0]
        index = json.loads(prompt.split("Selected issue index (untrusted task data):\n")[1].split("\n")[0])
        self.assertEqual(index[0]["choice"], f"{HAIKU}/default")
        self.assertNotIn("choice", index[1])
        self.assertEqual(result["jobs"][1]["choice"]["build"]["model"], SONNET)
        self.assertNotIn("SECRET-LABEL", prompt)

    def test_the_planner_prompt_asks_for_separate_jobs_per_choice(self):
        text = (Path(planner.__file__).parent / "prompts" / "planner.md").read_text()
        self.assertIn("never group issues whose `choice` differs", text)
        self.assertIn("separate jobs", text)

    def test_a_refused_label_blocks_the_issue_and_nothing_else_is_planned_for_it(self):
        self.serve(n1=entry("one", "factory-model:gpt"), n2=entry("two"))
        self.answer(2)
        result = self.run_plan([1, 2])
        self.assertEqual([job["id"] for job in result["jobs"]], ["fix-2"])
        by_issue = {d["issue"]: d for d in result["dispositions"]}
        self.assertEqual(by_issue[1]["status"], "blocked")
        self.assertEqual(by_issue[1]["reason"], blocked_reason(("model-not-allowed",)))
        self.assertEqual(by_issue[2]["status"], "job")
        validate_plan(result)
        # The refused issue's text never reached the planner: not in the index, not in its evidence file.
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn(f"{SECRET}one", self.prompts[0])
        self.assertIn(f"{SECRET}two", self.prompts[0])
        evidence = [p for p in self.paths[0] if p.name != "open-prs.json"][0]
        self.assertEqual(evidence.name, "issues-planned.json")
        self.assertEqual([i["number"] for i in json.loads(evidence.read_text())], [2])
        self.assertIn(str(evidence.resolve()), self.prompts[0])
        # The snapshot keeps both, so the plan lists every selected issue.
        self.assertEqual([i["number"] for i in result["issues"]], [1, 2])

    def test_when_every_issue_is_refused_no_planner_session_runs_and_no_usage_is_written(self):
        self.serve(n1=entry("one", "factory-model:gpt"), n2=entry("two", "factory-effort:max"), n3=entry("three", "factory-model:opus", "factory-model:sonnet"))
        result = self.run_plan([1, 2, 3])
        self.assertEqual(self.prompts, [])
        self.assertEqual(result["jobs"], [])
        self.assertEqual(
            {d["issue"]: (d["status"], d["reason"]) for d in result["dispositions"]},
            {
                1: ("blocked", blocked_reason(("model-not-allowed",))),
                2: ("blocked", blocked_reason(("effort-above-max",))),
                3: ("blocked", blocked_reason(("duplicate-model-label",))),
            },
        )
        validate_plan(result)
        self.assertFalse((self.root / "plan" / "planner-usage.json").exists())
        self.assertEqual(planner.planner_usage_records(self.root / "plan"), [])
        self.assertTrue((self.root / "plan" / "plan.json").is_file())

    def test_each_refusal_is_one_fixed_log_line_without_the_label_or_the_issue_text(self):
        self.serve(
            n1=entry("one", "factory-model:SECRET-LABEL-NAME"),
            n2=entry("two", "factory-effort:SECRET-LEVEL-NAME", "factory-model:opus", "factory-model:sonnet"),
        )
        self.run_plan([1, 2])
        self.assertEqual(
            self.err.splitlines(),
            [
                "factory: issue directive refused issue=1 reason=model-not-allowed",
                "factory: issue directive refused issue=2 reason=duplicate-model-label,unknown-effort",
            ],
        )
        # plan.json keeps the captured issue snapshot as before; what Factory itself writes about the
        # refusal (the dispositions) and every log and prompt line is fixed text.
        saved = json.loads((self.root / "plan" / "plan.json").read_text())
        written = json.dumps(saved["dispositions"])
        for text in (self.out, self.err, json.dumps(self.prompts), written):
            self.assertNotIn("SECRET-LABEL-NAME", text)
            self.assertNotIn("SECRET-LEVEL-NAME", text)
        self.assertNotIn(SECRET, self.out + self.err)

    def test_a_label_nobody_may_apply_blocks_the_issue_without_a_planner_session(self):
        self.serve(n1=entry("one", "factory-model:opus", "factory-effort:high"), n2=entry("two"))
        self.answer(2)
        self.authority = lambda number, labels: False
        result = self.run_plan([1, 2])
        self.assertEqual([job["id"] for job in result["jobs"]], ["fix-2"])
        by_issue = {d["issue"]: d for d in result["dispositions"]}
        self.assertEqual((by_issue[1]["status"], by_issue[1]["reason"]), ("blocked", blocked_reason(("label-not-authorised",))))
        self.assertEqual(by_issue[2]["status"], "job")
        self.assertEqual(result["jobs"][0]["choice"]["build"]["model_source"], "built-in")
        self.assertNotIn(f"{SECRET}one", self.prompts[0])
        self.assertEqual(self.err, "factory: issue directive refused issue=1 reason=label-not-authorised\n")
        validate_plan(result)

    def test_without_an_authority_a_choice_label_is_refused(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        result = self.run_plan([1], authority=None)
        self.assertEqual((result["jobs"], self.prompts), ([], []))
        self.assertEqual(result["dispositions"][0]["reason"], blocked_reason(("label-not-authorised",)))

    def test_an_authority_that_cannot_answer_stops_the_plan_and_plans_nothing(self):
        self.serve(n1=entry("one", "factory-model:opus"), n2=entry("two"))
        self.answer(1, 2)

        def broken(number, labels):
            raise RuntimeError(trigger.AUTHORITY_UNKNOWN)

        self.authority = broken
        with self.assertRaises(RuntimeError):
            self.run_plan([1, 2])
        self.assertEqual(self.prompts, [])
        self.assertFalse((self.root / "plan" / "plan.json").exists())

    def test_a_directive_the_repository_does_not_allow_is_refused_not_replaced_by_a_default(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        options = AgentOptions(allowed_models=(SONNET,))
        result = self.run_plan([1], options)
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["dispositions"][0]["status"], "blocked")
        self.assertEqual(self.prompts, [])

    def test_an_effort_above_the_cap_is_refused_and_a_raised_cap_accepts_it(self):
        self.serve(n1=entry("one", "factory-effort:xhigh"))
        refused = self.run_plan([1], name="refused")
        self.assertEqual(refused["jobs"], [])
        self.assertEqual(refused["dispositions"][0]["reason"], blocked_reason(("effort-above-max",)))
        self.answer(1)
        accepted = self.run_plan([1], AgentOptions(max_effort="xhigh"), name="accepted")
        self.assertEqual(accepted["jobs"][0]["choice"]["build"]["effort"], "xhigh")
        self.assertEqual(accepted["jobs"][0]["choice"]["verify"]["effort_source"], "issue")

    def test_planner_output_that_covers_a_refused_issue_fails_the_plan(self):
        self.serve(n1=entry("one", "factory-model:gpt"), n2=entry("two"))
        self.answer(1, 2)
        with self.assertRaises(ValueError):
            self.run_plan([1, 2])

    def test_grouped_issues_with_different_labels_become_blocked_dispositions(self):
        self.serve(n1=entry("one", "factory-model:opus"), n2=entry("two", "factory-model:haiku"), n3=entry("three"))
        self.canned = {
            "version": 1,
            "jobs": [
                self.job_for(1, "both", issues=[1, 2]),
                self.job_for(3, "after", depends_on=["both"]),
            ],
            "dispositions": [
                {"issue": 1, "status": "job", "job": "both", "reason": "ok"},
                {"issue": 2, "status": "job", "job": "both", "reason": "ok"},
                {"issue": 3, "status": "job", "job": "after", "reason": "ok"},
            ],
        }
        result = self.run_plan([1, 2, 3])
        self.assertEqual(result["jobs"], [])
        self.assertEqual({d["issue"]: d["status"] for d in result["dispositions"]}, {1: "blocked", 2: "blocked", 3: "blocked"})
        validate_plan(result)

    def test_grouped_issues_with_the_same_label_run_as_one_job(self):
        self.serve(n1=entry("one", "factory-model:opus"), n2=entry("two", "factory-model:claude-opus-5-5"))
        self.canned = {
            "version": 1,
            "jobs": [self.job_for(1, "both", issues=[1, 2])],
            "dispositions": [
                {"issue": 1, "status": "job", "job": "both", "reason": "ok"},
                {"issue": 2, "status": "job", "job": "both", "reason": "ok"},
            ],
        }
        result = self.run_plan([1, 2])
        self.assertEqual(result["jobs"][0]["choice"]["build"]["model"], OPUS)

    def test_the_choice_labels_are_created_best_effort_and_a_failure_is_one_fixed_line(self):
        self.serve()
        options = AgentOptions(aliases={"fast": HAIKU})
        cfg = RepoConfig(repo="org/name")
        cli.ensure_choice_labels(self.work, cfg, options)
        created = [c for c in self.calls() if c[:2] == ["label", "create"]]
        self.assertEqual(
            sorted(c[2] for c in created),
            sorted([f"factory-model:{a}" for a in ("fast", "haiku", "opus", "sonnet")] + [f"factory-effort:{e}" for e in EFFORTS]),
        )
        self.assertTrue(all("--force" in c and c[-2:] == ["-R", "org/name"] for c in created))
        err = io.StringIO()
        with patch.object(cli, "create_label", side_effect=OSError(f"boom {SECRET}")), contextlib.redirect_stderr(err):
            cli.ensure_choice_labels(self.work, cfg, options)
        self.assertEqual(err.getvalue(), "factory: model label sync failed (OSError)\n")
        def lost():
            raise RuntimeError("lease lost")

        err = io.StringIO()
        with patch.object(cli, "create_label") as create, contextlib.redirect_stderr(err):
            cli.ensure_choice_labels(self.work, cfg, options, fence=lost)
        create.assert_not_called()
        self.assertEqual(err.getvalue(), "factory: model label sync failed (RuntimeError)\n")


class EpicWithChoiceLabelsTests(PlannerCase):
    """Epic mode and the label authority are two screens of one plan: neither may drop the other."""

    def bad_epic(self, number):
        bad = epic_disposition(number)
        bad["epic"]["phases"][0]["depends_on"] = [2]  # a forward dependency: the schema lets it through, the validator refuses it
        return bad

    def test_epic_mode_reaches_the_planner_through_the_screen(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        self.canned = {"version": 1, "jobs": [], "dispositions": [epic_disposition(1)]}
        result = self.run_plan([1], epic_on=True)
        self.assertEqual(result["dispositions"][0]["status"], "epic")
        self.assertIn("Epic mode is on", self.prompts[0])
        self.assertEqual(self.asked, [(1, ["factory-model:opus"])])
        result = self.run_plan([1], name="off")
        self.assertEqual(result["dispositions"][0], {"issue": 1, "status": "blocked", "reason": EPIC_REASON})
        self.assertNotIn("Epic mode", self.prompts[1])

    def test_an_invalid_epic_and_a_refused_label_are_both_blocked_and_the_plan_stands(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:opus"))
        self.canned = {"version": 1, "jobs": [], "dispositions": [self.bad_epic(1)]}
        self.authority = lambda number, labels: number != 2
        result = self.run_plan([1, 2], epic_on=True)
        by_issue = {d["issue"]: d for d in result["dispositions"]}
        self.assertEqual(by_issue[1], {"issue": 1, "status": "blocked", "reason": EPIC_REASON})
        self.assertEqual(by_issue[2]["reason"], blocked_reason(("label-not-authorised",)))
        self.assertNotIn(f"{SECRET}two", self.prompts[0])
        validate_plan(result)

    def test_when_every_issue_is_refused_nothing_runs_and_no_usage_is_written(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        self.authority = lambda number, labels: False
        result = self.run_plan([1], epic_on=True)
        self.assertEqual((result["jobs"], self.prompts), ([], []))
        self.assertEqual([d["status"] for d in result["dispositions"]], ["blocked"])
        self.assertFalse((self.root / "plan" / planner.USAGE_FILE).exists())
        validate_plan(result)


# A fake ``gh`` that answers what ``trigger.GithubReader`` asks (the repository, an issue's events and a
# user's role) from $FAKE_DIR/authority.json, and hands everything else to the plan tests' fake.
AUTHORITY_GH = r"""
import json as _json, os as _os, pathlib as _pathlib, re as _re, sys as _sys, urllib.parse as _url
_root = _pathlib.Path(_os.environ['FAKE_DIR'])
_args = _sys.argv[1:]
_route = next((a for a in _args[1:] if a.startswith('repos/')), '').partition('?')[0]
_known = _re.fullmatch(r'repos/org/name(/issues/\d+/events|/collaborators/[^/]+/permission)?', _route)
if _args[:1] == ['api'] and _known:
    with open(_root / 'calls.jsonl', 'a') as _log:
        _log.write('%s\n' % _json.dumps(_args))
    _data = _json.loads((_root / 'authority.json').read_text())
    if _data.get('broken'):
        _sys.stderr.write('HTTP 500 %s' % _os.environ['FAKE_SECRET'])
        _sys.exit(1)
    if _route == 'repos/org/name':
        print(_json.dumps({'archived': False, 'private': _data['private'], 'visibility': 'private' if _data['private'] else 'public'}))
    elif _route.endswith('/events'):
        if 'page=1' in _args[-3]:
            for _row in _data['events'].get(_route.split('/')[-2], []):
                print(_json.dumps(_row))
    else:
        _user = _url.unquote(_route.split('/')[-2])
        if _user not in _data['roles']:
            _sys.stderr.write('gh: Not Found (HTTP 404)')
            _sys.exit(1)
        print(_data['roles'][_user])
    _sys.exit(0)
"""


def labeled(label, login, kind="User", event="labeled"):
    return {"id": 1, "event": event, "label": label, "login": login, "type": kind}


class LabelAuthorityEndToEndTests(PlannerCase):
    """``planner.plan`` with the CLI's real authority over a fake GitHub: who applied a choice label decides."""

    ROLES = {"writer": "write", "maintainer": "maintain", "triager": "triage", "reader": "read", "outsider": "none"}

    def setUp(self):
        super().setUp()
        gh = self.root / "bin" / "gh"
        gh.write_text(f"#!{sys.executable}\n{AUTHORITY_GH}{gh.read_text().split(chr(10), 1)[1]}")
        self.events, self.private, self.broken = {}, True, False
        self.cfg = RepoConfig(repo="org/name")
        self.save()

    def save(self):
        data = {"events": self.events, "roles": self.ROLES, "private": self.private, "broken": self.broken}
        (self.root / "authority.json").write_text(json.dumps(data))

    def plan_with_real_authority(self, numbers, cfg=None):
        self.save()
        return self.run_plan(numbers, authority=cli.label_authority(cfg or self.cfg, self.work))

    def reads(self, kind):
        return [c for c in self.calls() if c[:1] == ["api"] and any(f"/{kind}" in a for a in c)]

    def assertBlocked(self, result, number):
        by_issue = {d["issue"]: d for d in result["dispositions"]}
        self.assertEqual((by_issue[number]["status"], by_issue[number]["reason"]), ("blocked", blocked_reason(("label-not-authorised",))))
        self.assertNotIn(number, [n for job in result["jobs"] for n in job["issues"]])

    def test_a_triage_user_cannot_set_the_model_of_an_issue_a_maintainer_queued(self):
        self.serve(n1=entry("one", "factory-build", "factory-model:opus", "factory-effort:high"))
        self.events = {"1": [labeled("factory-build", "writer"), labeled("factory-model:opus", "triager"), labeled("factory-effort:high", "triager")]}
        result = self.plan_with_real_authority([1])
        self.assertBlocked(result, 1)
        self.assertEqual((result["jobs"], self.prompts), ([], []))
        self.assertEqual(self.err, "factory: issue directive refused issue=1 reason=label-not-authorised\n")

    def test_a_user_with_write_access_or_more_may_choose(self):
        for number, login, role in ((1, "writer", "write"), (2, "maintainer", "maintain")):
            self.serve(**{f"n{number}": entry("one", "factory-model:opus", "factory-effort:low")})
            self.events = {str(number): [labeled("factory-model:opus", login), labeled("factory-effort:low", login)]}
            self.answer(number)
            with self.subTest(role):
                result = self.plan_with_real_authority([number])
                self.assertEqual(result["jobs"][0]["choice"]["build"]["model"], OPUS)
                self.assertEqual(result["jobs"][0]["choice"]["verify"]["effort_source"], "issue")
                self.assertEqual(self.err, "")

    def test_roles_below_write_unknown_users_and_bots_are_refused(self):
        for login, kind in (("triager", "User"), ("reader", "User"), ("outsider", "User"), ("ghost", "User"), ("writer", "Bot"), ("", "User")):
            self.serve(n1=entry("one", "factory-model:opus"))
            self.events = {"1": [labeled("factory-model:opus", login, kind)]}
            with self.subTest(login=login, kind=kind):
                self.assertBlocked(self.plan_with_real_authority([1]), 1)

    def test_a_label_with_no_labeled_event_is_refused(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        self.events = {"1": [labeled("factory-build", "writer"), labeled("factory-model:haiku", "writer")]}
        self.assertBlocked(self.plan_with_real_authority([1]), 1)

    def test_the_newest_applier_of_a_label_counts(self):
        self.serve(n1=entry("one", "factory-model:opus"))
        removed = labeled("factory-model:opus", "writer", event="unlabeled")
        self.events = {"1": [labeled("factory-model:opus", "writer"), removed, labeled("factory-model:opus", "triager")]}
        self.assertBlocked(self.plan_with_real_authority([1]), 1)
        self.events = {"1": [labeled("factory-model:opus", "triager"), removed, labeled("factory-model:opus", "writer")]}
        self.answer(1)
        self.assertEqual(self.plan_with_real_authority([1])["jobs"][0]["choice"]["build"]["model"], OPUS)

    def test_every_choice_label_must_come_from_someone_who_may_choose(self):
        self.serve(n1=entry("one", "factory-model:opus", "factory-effort:low"))
        self.events = {"1": [labeled("factory-model:opus", "writer"), labeled("factory-effort:low", "triager")]}
        self.assertBlocked(self.plan_with_real_authority([1]), 1)

    def test_the_minimum_of_the_repository_is_the_bar_and_below_write_needs_a_private_repository(self):
        self.serve(n1=entry("one", "factory-effort:low"))
        self.events = {"1": [labeled("factory-effort:low", "triager")]}
        lowered = RepoConfig(repo="org/name", auto_build_min_permission="triage")
        self.answer(1)
        self.assertEqual(self.plan_with_real_authority([1], lowered)["jobs"][0]["choice"]["build"]["effort"], "low")
        self.private = False  # the trigger refuses a minimum below write on a public repository, and so does this
        self.assertBlocked(self.plan_with_real_authority([1], lowered), 1)
        raised = RepoConfig(repo="org/name", auto_build_min_permission="admin")
        self.private = True
        self.events = {"1": [labeled("factory-effort:low", "maintainer")]}
        self.assertBlocked(self.plan_with_real_authority([1], raised), 1)

    def test_a_failed_read_stops_the_plan_with_a_fixed_message_and_plans_nothing(self):
        self.serve(n1=entry("one", "factory-model:opus"), n2=entry("two"))
        self.broken = True
        self.answer(1, 2)
        with self.assertRaises(RuntimeError) as caught:
            self.plan_with_real_authority([1, 2])
        self.assertEqual(str(caught.exception), trigger.AUTHORITY_UNKNOWN)
        self.assertNotIn(SECRET, str(caught.exception) + self.out + self.err)
        self.assertEqual(self.prompts, [])
        self.assertFalse((self.root / "plan" / "plan.json").exists())

    def test_github_is_asked_only_for_labelled_issues_once_per_issue_and_once_per_user(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:opus", "factory-effort:low"), n3=entry("three", "factory-effort:high"))
        self.events = {
            "2": [labeled("factory-model:opus", "writer"), labeled("factory-effort:low", "writer")],
            "3": [labeled("factory-effort:high", "writer")],
        }
        self.answer(1, 2, 3)
        result = self.plan_with_real_authority([1, 2, 3])
        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual(len(self.reads("events")), 2)  # nothing for the unlabelled issue, one read for two labels
        self.assertEqual(len(self.reads("permission")), 1)  # the same user is asked about once
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["api", "repos/org/name"]]), 1)  # the visibility, once

    def test_no_choice_label_means_no_extra_github_call(self):
        self.serve(n1=entry("one", "bug"), n2=entry("two", "factory-build"))
        self.answer(1, 2)
        self.plan_with_real_authority([1, 2])
        self.assertEqual([c for c in self.calls() if c[:1] == ["api"] and not any("/issues/" in a for a in c)], [])

    def test_the_plan_and_append_commands_hand_the_repositorys_authority_to_the_planner(self):
        cfg = RepoConfig(repo="org/name", auto_build_min_permission="maintain")
        authority = cli.label_authority(cfg, self.work)
        self.assertIsInstance(authority, trigger.LabelAuthority)
        self.assertEqual(authority.minimum, "maintain")
        self.assertEqual(authority.reader.repo, "org/name")
        self.assertIsNone(cli.label_authority(RepoConfig(), self.work).reader.repo)
        args = SimpleNamespace(issues=[1], limit=None, timeout=10, use_snapshot=False)
        directory = self.root / "run"
        directory.mkdir()
        stop = RuntimeError("stop here")
        with (
            patch.object(cli, "planner_worktree", return_value=self.work),
            patch.object(cli, "share_planner_tree"),
            patch.object(cli, "plan", side_effect=stop) as plan,
        ):
            with self.assertRaises(RuntimeError):
                cli.plan_command(args, cfg, self.work, directory, None)
        handed = plan.call_args.kwargs["authority"]
        self.assertIsInstance(handed, trigger.LabelAuthority)
        self.assertEqual(handed.minimum, "maintain")


class BlockedCommentTests(CommentGhCase):
    def test_a_refused_label_is_reported_through_the_issue_commenter_without_the_label(self):
        reason = blocked_reason(directive("factory-model:SECRET-LABEL-NAME", "factory-effort:max"))
        plan = plan_with({"issue": 1, "status": "blocked", "reason": reason})
        labels, _ = self.labeller(plan)
        labels.sync_plan()
        (comment,) = self.comments(1)
        body = comment["body"]
        self.assertTrue(body.startswith(f"{marker(1, 'blocked')}\n"))
        self.assertIn("**Factory: blocked**", body)
        self.assertIn("allowed_models", body)
        self.assertIn("max_effort", body)
        self.assertIn("Code: `blocked`", body)
        self.assertNotIn("SECRET-LABEL-NAME", body)


# ---------------------------------------------------------------------------
# The append path
# ---------------------------------------------------------------------------


class AppendChoiceTests(AppendCase):
    def setUp(self):
        super().setUp()
        self.authority = lambda number, labels: True
        self.built = []

    def append(self, *args, **kwargs):
        def build(cfg, cwd):
            self.built.append((cfg, cwd))
            return self.authority

        with patch.object(cli, "label_authority", side_effect=build):
            return super().append(*args, **kwargs)

    def test_a_label_whose_applier_may_not_choose_is_blocked_in_the_fragment_and_no_planner_runs(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:opus"), n3=entry("three"))
        self.authority = lambda number, labels: False
        self.append()
        self.assertEqual(self.prompts, [])
        (path,) = self.fragments()
        _, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual(jobs, [])
        self.assertEqual(dispositions, [{"issue": 2, "status": "blocked", "reason": blocked_reason(("label-not-authorised",))}])
        self.assertEqual(self.err, "factory: issue directive refused issue=2 reason=label-not-authorised\n")
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.built[0][0], self.cfg)

    def test_epic_mode_and_the_label_authority_both_apply_to_an_append(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:opus"), n3=entry("three"))
        bad = epic_disposition(3)
        bad["epic"]["phases"][0]["depends_on"] = [2]
        self.result = {"version": 1, "jobs": [], "dispositions": [bad]}
        self.authority = lambda number, labels: number != 2
        self.append(issues=(2, 3))
        self.assertIn("Epic mode is on", self.prompts[0])
        (path,) = self.fragments()
        _, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual(jobs, [])
        self.assertEqual(
            {d["issue"]: d["reason"] for d in dispositions},
            {2: blocked_reason(("label-not-authorised",)), 3: EPIC_REASON},
        )
        self.assertEqual({d["status"] for d in dispositions}, {"blocked"})

    def test_a_valid_epic_of_an_issue_without_labels_is_kept_in_the_fragment(self):
        self.result = {"version": 1, "jobs": [], "dispositions": [epic_disposition(2)]}
        self.append()
        (path,) = self.fragments()
        _, _, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual([d["status"] for d in dispositions], ["epic"])


    def test_an_appended_job_carries_the_choice_read_from_the_fresh_labels(self):
        self.serve(
            n1=entry("one"), n2=entry("two", "factory-model:opus", "factory-effort:low"), n3=entry("three")
        )
        self.append()
        (path,) = self.fragments()
        _, jobs, _ = fragments.parse(path.read_bytes(), 10)
        self.assertEqual(jobs[0]["choice"]["build"]["model"], OPUS)
        self.assertEqual(jobs[0]["choice"]["verify"]["effort"], "low")
        self.assertEqual(jobs[0]["choice"]["build"]["model_source"], "issue")
        self.assertEqual(self.err, "")
        # The supervisor applies it and the recorded choice lands in plan.json.
        with contextlib.redirect_stderr(io.StringIO()):
            self.runner.ingest_fragments()
        saved = json.loads((self.directory / "plan.json").read_text())
        validate_plan(saved)
        self.assertEqual(saved["jobs"][1]["choice"]["build"]["model"], OPUS)
        self.assertEqual(self.runner.jobs["fix-b"]["choice"]["build"]["model"], OPUS)

    def test_an_appended_issue_without_labels_gets_the_defaults_and_a_planner_choice_is_dropped(self):
        self.result["jobs"][0]["choice"] = {"build": {"model": "planner-picked"}}
        self.append()
        (path,) = self.fragments()
        _, jobs, _ = fragments.parse(path.read_bytes(), 10)
        self.assertEqual(jobs[0]["choice"]["build"]["model"], SONNET)
        self.assertEqual(jobs[0]["choice"]["build"]["model_source"], "built-in")

    def test_a_refused_label_becomes_a_blocked_disposition_in_the_fragment_and_no_planner_runs(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:gpt"), n3=entry("three"))
        self.append()
        self.assertEqual(self.prompts, [], "no session for an issue that is refused")
        (path,) = self.fragments()
        issues, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual(jobs, [])
        self.assertEqual(dispositions, [{"issue": 2, "status": "blocked", "reason": blocked_reason(("model-not-allowed",))}])
        self.assertEqual([i["number"] for i in issues], [2])
        self.assertEqual(planner.planner_usage_records(self.directory), [], "no planner ran, so no usage record")
        self.assertEqual(self.err, "factory: issue directive refused issue=2 reason=model-not-allowed\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.runner.ingest_fragments()
        saved = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual({d["issue"]: d["status"] for d in saved["dispositions"]}, {1: "job", 2: "blocked"})
        self.assertEqual([j["id"] for j in saved["jobs"]], ["fix-a"])

    def test_the_allowed_models_of_the_repository_apply_to_an_append(self):
        self.serve(n1=entry("one"), n2=entry("two", "factory-model:opus"), n3=entry("three"))
        cfg = RepoConfig(repo="org/name", allowed_models=(SONNET,))
        self.append(cfg=cfg)
        (path,) = self.fragments()
        _, jobs, dispositions = fragments.parse(path.read_bytes(), 10)
        self.assertEqual((jobs, [d["status"] for d in dispositions]), ([], ["blocked"]))


# ---------------------------------------------------------------------------
# The worker invocation, status and the pull request
# ---------------------------------------------------------------------------


class WorkerInvocationTests(AgentTestCase):
    def argv(self, phase, **kwargs):
        self.stub(DUMP_ARGV)
        self.run_phase(phase, **kwargs)
        argv = self.seen()["argv"]
        return argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]

    def test_the_recorded_choice_reaches_the_claude_command_line(self):
        choice = {"model": "claude-test-model", "effort": "xhigh"}
        self.assertEqual(self.argv("build-0", choice=choice), ("claude-test-model", "xhigh"))
        self.assertEqual(self.argv("verify", choice=choice), ("claude-test-model", "xhigh"))

    def test_no_choice_means_the_options_then_the_built_in_table(self):
        self.assertEqual(self.argv("build-0"), (SONNET, "high"))
        options = AgentOptions(default_model="m-default", default_effort="low")
        self.assertEqual(self.argv("build-0", options=options), ("m-default", "low"))
        self.assertEqual(self.argv("preflight", options=options), (HAIKU, "low"))

    def test_the_choice_wins_over_the_repository_options(self):
        options = AgentOptions(models={"build": "m-phase"}, efforts={"build": "low"}, default_model="m-default")
        self.assertEqual(self.argv("build-0", options=options, choice={"model": "m-issue", "effort": "max"}), ("m-issue", "max"))

    def test_a_malformed_choice_is_refused_before_spawning(self):
        with self.assertRaises(ValueError):
            self.run_phase("build-0", choice={"effort": "extreme"})
        self.assertFalse((self.binary / "seen.json").exists())


class RunnerInvocationTests(unittest.TestCase):
    """``Runner.invoke`` hands the recorded choice to build and verify attempts only."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        plan = assignment()
        plan["jobs"][0]["choice"] = job_choice(AgentOptions(), Directive(OPUS, "low"))
        self.runner = Runner(self.directory, self.directory, plan, cfg=RepoConfig(repo="org/name", model="m-default"))
        self.runner.issue_labels.sync = lambda wanted: None
        self.runner.state.save()
        self.calls = []

    def invoke(self, phase):
        def fake(prompt, cwd, prefix, name, *args, **kwargs):
            self.calls.append((name, kwargs.get("choice"), kwargs.get("options")))
            return {"ok": True}, {"total_cost_usd": 0.0}

        with patch("factory.runner.run_agent", side_effect=fake), patch.object(Runner, "guidance", return_value=""):
            self.runner.invoke("fix-a", phase, "prompt", self.directory)
        return self.calls[-1]

    def test_build_verify_and_the_baseline_session_take_the_recorded_choice(self):
        for phase in ("build-0", "build-2", "verify", "verify-baseline"):
            name, choice, _ = self.invoke(phase)
            self.assertEqual((name, choice), (phase, {"model": OPUS, "effort": "low"}), phase)

    def test_review_never_takes_an_issue_choice_and_keeps_the_repository_options(self):
        name, choice, options = self.invoke("review-0")
        self.assertIsNone(choice)
        self.assertEqual(policy_for(name, options).model, "m-default")
        self.assertEqual(policy_for("review-0", options, choice).model, "m-default")

    def test_a_job_planned_before_choices_were_recorded_resolves_from_the_live_options(self):
        del self.runner.jobs["fix-a"]["choice"]
        name, choice, options = self.invoke("build-0")
        self.assertIsNone(choice)
        self.assertEqual(policy_for(name, options).model, "m-default")

    def test_a_resumed_run_keeps_the_recorded_values_when_the_configuration_changes(self):
        later = AgentOptions(default_model="m-changed", aliases={"opus": "m-other-opus"})
        self.runner.options = later
        name, choice, options = self.invoke("build-1")
        self.assertEqual(policy_for(name, options, choice).model, OPUS)


class RunnerToCommandLineTests(AgentTestCase):
    """The whole chain without a fake in the middle: recorded choice, ``Runner.invoke``, ``run_agent``, argv."""

    def argv_of(self, runner, phase):
        self.stub(DUMP_ARGV)
        with patch.object(Runner, "guidance", return_value=""):
            runner.invoke("fix-a", phase, "prompt", self.cwd)
        argv = self.seen()["argv"]
        return argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]

    def runner(self, cfg, record=True):
        plan = assignment()
        if record:
            plan["jobs"][0]["choice"] = job_choice(cfg.agent_options(), Directive(OPUS, "low"))
        directory = self.root / "run"
        directory.mkdir()
        runner = Runner(directory, directory, plan, cfg=cfg)
        runner.issue_labels.sync = lambda wanted: None
        runner.state.save()
        return runner

    def test_the_labelled_choice_reaches_build_and_verify_and_never_the_reviewer(self):
        runner = self.runner(RepoConfig(repo="org/name", model="m-default", effort="medium"))
        self.assertEqual(self.argv_of(runner, "build-0"), (OPUS, "low"))
        self.assertEqual(self.argv_of(runner, "verify"), (OPUS, "low"))
        self.assertEqual(self.argv_of(runner, "review-0"), ("m-default", "medium"))

    def test_a_repository_with_only_models_runs_every_phase_as_before(self):
        cfg = RepoConfig(repo="org/name", models={"review": "claude-test-review"})
        runner = self.runner(cfg)
        # No label: the recorded choice is the repository's own resolution, i.e. the built-in table.
        runner.jobs["fix-a"]["choice"] = job_choice(cfg.agent_options())
        self.assertEqual(self.argv_of(runner, "build-0"), (SONNET, "high"))
        self.assertEqual(self.argv_of(runner, "verify"), (SONNET, "medium"))
        self.assertEqual(self.argv_of(runner, "review-0"), ("claude-test-review", "high"))
        del runner.jobs["fix-a"]["choice"]
        self.assertEqual(self.argv_of(runner, "build-0"), (SONNET, "high"))


class StatusAndPullRequestTests(unittest.TestCase):
    def plan(self, *, record=True, options=None):
        plan = assignment()
        if record:
            plan["jobs"][0]["choice"] = job_choice(options or AgentOptions(), Directive(OPUS, "low"))
        return plan

    def test_status_shows_the_models_with_their_sources(self):
        plan = self.plan()
        report = status_report(plan, {"jobs": {"fix-a": {"status": "queued"}}}, now="2026-11-08T00:00:00+00:00")
        self.assertEqual(report["jobs"][0]["models"], plan["jobs"][0]["choice"])
        text = format_status(report)
        self.assertIn(f"  Models: build {OPUS} (issue), effort low (issue); verify {OPUS} (issue), effort low (issue)", text)
        report["jobs"][0]["models"]["build"]["model"] = "tampered"
        self.assertEqual(plan["jobs"][0]["choice"]["build"]["model"], OPUS, "the report holds a copy")

    def test_status_of_a_job_without_a_record_has_no_models_line(self):
        report = status_report(self.plan(record=False), {"jobs": {"fix-a": {"status": "queued"}}}, now="2026-11-08T00:00:00+00:00")
        self.assertIsNone(report["jobs"][0]["models"])
        self.assertNotIn("Models:", format_status(report))

    def body(self, plan, options=None):
        runner = Runner.__new__(Runner)
        runner.directory = Path("run")
        runner.options = options or AgentOptions()
        job = plan["jobs"][0]
        return runner.pr_body(
            job, {"commit": "a" * 40}, {"summary": "Done", "tests": ["t"]}, {"pass": True, "findings": []}, {"complete": True}, True
        )

    def test_the_pull_request_names_the_models_and_where_each_came_from(self):
        body = self.body(self.plan(), AgentOptions(models={"review": "m-review"}))
        self.assertEqual(body.splitlines()[0], "Closes #1")
        section = body.split("## Models\n\n")[1].split("\n## ")[0]
        self.assertEqual(
            section.splitlines(),
            [
                f"- build: {OPUS} (issue label), effort low (issue label)",
                f"- verify: {OPUS} (issue label), effort low (issue label)",
                "- review: m-review (repository per-phase setting), effort high (built-in default)",
            ],
        )
        self.assertLess(body.index("## Changed scope"), body.index("## Models"))
        self.assertLess(body.index("## Models"), body.index("## Validation"))

    def test_repository_sources_are_named_too(self):
        options = AgentOptions(default_model="m-default", default_effort="medium")
        plan = assignment()
        plan["jobs"][0]["choice"] = job_choice(options)
        section = self.body(plan, options).split("## Models\n\n")[1].split("\n## ")[0]
        self.assertIn("- build: m-default (repository default), effort medium (repository default)", section)
        self.assertIn("- verify: m-default (repository default), effort medium (repository default)", section)
        self.assertIn("- review: m-default (repository default), effort medium (repository default)", section)

    def test_a_job_without_a_record_has_no_models_block(self):
        self.assertNotIn("## Models", self.body(self.plan(record=False)))

    def test_the_block_holds_only_validated_ids_and_fixed_words(self):
        plan = assignment()
        plan["jobs"][0]["choice"] = job_choice(AgentOptions(), Directive("vendor.model:x@1", "low"))
        block = models_block(model_choice.recorded_choice(plan["jobs"][0]), AgentOptions(), lambda value: value)
        self.assertIn("vendor.model:x@1", block)
        self.assertNotIn(SECRET, block)


if __name__ == "__main__":
    unittest.main()
