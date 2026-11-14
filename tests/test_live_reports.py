"""tools/live_reports.py: the reports over files a person saved from the live runbook (slice 21 of the pod backend).

These commands call nothing, so the tests give them files. What they prove is the judgement of each step (a verdict that fails when the
protection is removed) and that nothing but fixed text, counts and the repository's own action names is printed.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from factory import podcontract as pc
from tests.live_tools import calls, imported_modules, load

rep = load("live_reports")


class Files(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def file(self, name, content):
        path = Path(self.directory.name, name)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return str(path)

    def run_tool(self, *argv):
        lines = []
        code = rep.main(list(argv), emit=lines.append)
        return code, lines


def entry(invocations=25, rss=1000.0, p95=900.0, cpu=100.0, p95cpu=90.0):
    return {"invocations": invocations, "peak_rss_mb": rss, "p95_rss_mb": p95, "peak_cpu_seconds": cpu, "p95_cpu_seconds": p95cpu}


class Footprint(Files):
    def test_a_phase_that_fits_a_class_passes_and_says_which(self):
        status = self.file("a.json", {"footprint": {"build": entry(rss=1630.2, p95=1411.0, cpu=212.4, p95cpu=180.3)}})
        code, lines = self.run_tool("footprint", status)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[0], "phase build: invocations 25, peak rss 1630.2 MiB, p95 rss (upper bound) 1411.0 MiB, peak cpu 212.4 s, p95 cpu (upper bound) 180.3 s")
        self.assertIn("phase build class small: peak rss below the memory limit of 2048 MiB", lines)
        self.assertIn("phase build class standard: peak rss below the memory limit of 4096 MiB", lines)

    def test_a_peak_between_the_two_classes_fits_only_the_larger(self):
        _, lines = self.run_tool("footprint", self.file("a.json", {"footprint": {"build": entry(rss=3000.0)}}))
        self.assertIn("phase build class small: peak rss NOT below the memory limit of 2048 MiB", lines)
        self.assertIn("phase build class standard: peak rss below the memory limit of 4096 MiB", lines)

    def test_a_peak_equal_to_a_limit_does_not_fit_that_class(self):
        _, lines = self.run_tool("footprint", self.file("a.json", {"footprint": {"build": entry(rss=2048.0)}}))
        self.assertIn("phase build class small: peak rss NOT below the memory limit of 2048 MiB", lines)
        self.assertIn("phase build class standard: peak rss below the memory limit of 4096 MiB", lines)

    def test_a_peak_above_the_largest_class_fails(self):
        code, lines = self.run_tool("footprint", self.file("a.json", {"footprint": {"build": entry(rss=4096.0)}}))
        self.assertEqual(code, 1)
        self.assertIn("FAIL phase build: no resource class has a memory limit above the peak", lines)

    def test_files_are_combined_peak_is_the_maximum_and_p95_an_upper_bound(self):
        a = self.file("a.json", {"footprint": {"build": entry(10, 1000.0, 800.0)}})
        b = self.file("b.json", {"footprint": {"build": entry(15, 1500.0, 700.0), "review": entry(3)}})
        _, lines = self.run_tool("footprint", a, b)
        self.assertTrue(lines[0].startswith("phase build: invocations 25, peak rss 1500.0 MiB, p95 rss (upper bound) 800.0 MiB"))
        self.assertIn("note: phase review has fewer than 20 invocations: not a measurement", lines)
        self.assertNotIn("note: phase build has fewer than 20 invocations: not a measurement", lines)

    def test_a_fewer_than_twenty_sessions_phase_is_noted_not_failed(self):
        code, lines = self.run_tool("footprint", self.file("a.json", {"footprint": {"build": entry(19)}}))
        self.assertEqual(code, 0)
        self.assertIn("note: phase build has fewer than 20 invocations: not a measurement", lines)

    def test_a_file_that_is_not_a_status_report_fails_closed(self):
        for content in ("not json", {"nothing": 1}, {"footprint": {"build": {"invocations": 1}}}, {"footprint": {"build": entry(rss=True)}}, {"footprint": {}}, [1]):
            code, lines = self.run_tool("footprint", self.file("x.json", content))
            with self.subTest(content=str(content)[:30]):
                self.assertEqual(code, 1)
                self.assertTrue(lines[0].startswith("FAIL footprint"), lines)
        self.assertEqual(self.run_tool("footprint", str(Path(self.directory.name, "missing")))[0], 1)

    def test_the_class_limits_are_the_contracts(self):
        self.assertEqual({name: rep.mib(spec["memory"]) for name, spec in pc.CLASSES.items()}, {"standard": 4096, "small": 2048})

    def test_the_shape_it_reads_is_the_one_status_prints(self):
        from factory import status

        report = {"footprint": status.footprint_summary([{"usage": [{"phase": "build", "footprint": {"peak_rss_mb": 1200.5, "cpu_seconds": 30.0}}]}])}
        code, lines = self.run_tool("footprint", self.file("real.json", report))
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("phase build: invocations 1, peak rss 1200.5 MiB"))


def role(actions, name="Factory Plan Cluster Reader"):
    return [{"roleName": name, "permissions": [{"actions": [], "dataActions": actions}], "assignableScopes": ["/subscriptions/x"]}]


class DisplayFilter(Files):
    def test_a_phase_name_from_the_status_is_printed_without_a_control_or_shell_character(self):
        hostile = "build\x1b[31m$(id)`x`;\"q\"\nnext-line-is-long-enough-to-be-cut"
        code, lines = self.run_tool("footprint", self.file("a.json", {"footprint": {hostile: entry(invocations=3)}}))
        self.assertEqual(code, 0, lines)
        text = "\n".join(lines)
        self.assertNotRegex(text, r"[\x00-\x09\x0b-\x1f\x7f$`;\"\\]")
        self.assertIn("phase build?[31m?(id)?x??", text)
        self.assertNotIn("long-enough", text)  # cut at 20 characters
        for line in lines:
            self.assertRegex(line, r"^(phase |note: phase |footprint: )")

    def test_an_action_name_that_is_not_a_plain_name_is_not_printed_at_all(self):
        live = {"roleName": "x", "permissions": [{"dataActions": ["Microsoft.X/y/read", "Evil$(id)/z\x1b[31m"]}]}
        _, lines = self.run_tool("role-diff", self.file("r.json", [live]))
        self.assertNotRegex("\n".join(lines), r"[$\x1b]")
        self.assertIn("extra in the live role: <unprintable>", lines)


class RoleDiff(Files):
    REPO = json.loads(rep.ROLE_FILE.read_text())

    def test_a_live_role_equal_to_the_repository_file_passes(self):
        code, lines = self.run_tool("role-diff", self.file("r.json", role(list(reversed(self.REPO["DataActions"])))))
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[0], f"repository role: {len(self.REPO['DataActions'])} data actions; live role: {len(self.REPO['DataActions'])} data actions")
        self.assertEqual(lines[-1], "role-diff: the live role equals the repository file")

    def test_a_live_role_that_lacks_the_new_reads_names_each_missing_action(self):
        old = [a for a in self.REPO["DataActions"] if not a.endswith(("/roles/read", "/rolebindings/read"))]
        code, lines = self.run_tool("role-diff", self.file("r.json", role(old)))
        self.assertEqual(code, 1)
        missing = [line for line in lines if line.startswith("missing in the live role: ")]
        self.assertEqual(len(missing), 2)
        self.assertTrue(any(line.endswith("rbac.authorization.k8s.io/roles/read") for line in missing))
        self.assertEqual(lines[-1], "FAIL role-diff: 2 differences")

    def test_an_extra_action_in_the_live_role_is_reported_too(self):
        extra = "Microsoft.ContainerService/managedClusters/secrets/read"
        code, lines = self.run_tool("role-diff", self.file("r.json", role(self.REPO["DataActions"] + [extra])))
        self.assertEqual(code, 1)
        self.assertIn(f"extra in the live role: {extra}", lines)

    def test_it_reads_one_definition_or_a_list_of_one_and_nothing_else(self):
        one = self.file("one.json", role(self.REPO["DataActions"])[0])
        self.assertEqual(self.run_tool("role-diff", one)[0], 0)
        for content in ("x", [], [{}, {}], {"permissions": [{"dataActions": "no"}]}):
            code, lines = self.run_tool("role-diff", self.file("bad.json", content))
            self.assertEqual((code, lines[0].startswith("FAIL role-diff")), (1, True), content)

    def test_the_production_name_is_called_out(self):
        _, lines = self.run_tool("role-diff", self.file("r.json", role(self.REPO["DataActions"])))
        self.assertIn("role name: the production name", lines)
        _, lines = self.run_tool("role-diff", self.file("r.json", role(self.REPO["DataActions"], "Factory Plan Cluster Reader mirror1")))
        self.assertNotIn("role name: the production name", lines)

    def test_the_repository_role_has_no_admission_action_because_azure_defines_none(self):
        self.assertFalse([a for a in self.REPO["DataActions"] if "admissionregistration" in a])


class MirrorRole(Files):
    SUB = "11111111-2222-3333-4444-555555555555"

    def build(self, **override):
        options = {"prefix": "mirror1", "subscription": self.SUB, "resource_group": "rg-mirror1"}
        options.update(override)
        return self.run_tool("mirror-role", *(arg for key, value in options.items() for arg in (f"--{key.replace('_', '-')}", value)))

    def test_the_name_cannot_be_the_production_role_and_the_scope_is_the_mirrors_group(self):
        code, lines = self.build()
        self.assertEqual(code, 0)
        document = json.loads("\n".join(lines))
        production = json.loads(rep.ROLE_FILE.read_text())
        self.assertNotEqual(document["Name"], production["Name"])
        self.assertEqual(document["Name"], "Factory Plan Cluster Reader mirror1")
        self.assertEqual(document["AssignableScopes"], [f"/subscriptions/{self.SUB}/resourceGroups/rg-mirror1"])
        self.assertEqual(document["DataActions"], production["DataActions"])
        self.assertEqual(production["AssignableScopes"], ["/subscriptions/<<SUBSCRIPTION_ID>>/resourceGroups/<<RESOURCE_GROUP>>"])  # the file is not changed

    def test_a_bad_prefix_subscription_or_group_is_refused_and_prints_no_json(self):
        for override in ({"prefix": "Mirror"}, {"prefix": "m"}, {"prefix": "a--b"}, {"prefix": ""}, {"subscription": "not-a-guid"}, {"resource_group": "rg x"}, {"resource_group": "rg;x"}):
            code, lines = self.build(**override)
            with self.subTest(override):
                self.assertEqual(code, 2)
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("FAIL mirror-role"))


SUMMARY = "Plan: 5 to add, 1 to change, 0 to destroy."
CLEAN = f"kubernetes_namespace_v1.worker[0] will be created\n  # kubernetes_namespace_v1.worker[0] will be created\n\n{SUMMARY}\n"


class PlanLog(Files):
    def test_a_plan_with_a_summary_and_no_problem_passes(self):
        code, lines = self.run_tool("plan-log", self.file("p.txt", CLEAN))
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines, ["plan summary: 5 to add, 1 to change, 0 to destroy", "errors: 0, forbidden mentions: 0, destroy or replace lines: 0", "plan-log: passed"])

    def test_no_changes_is_a_summary_and_is_what_expect_no_changes_wants(self):
        path = self.file("p.txt", "No changes. Your infrastructure matches the configuration.\n")
        self.assertEqual(self.run_tool("plan-log", path, "--expect-no-changes")[0], 0)
        self.assertEqual(self.run_tool("plan-log", self.file("q.txt", CLEAN), "--expect-no-changes")[0], 1)

    def test_a_forbidden_read_fails_the_gate(self):
        text = CLEAN + '\nError: Failed to read resource\n  validatingadmissionpolicies.admissionregistration.k8s.io is forbidden: User "x" cannot get\n'
        code, lines = self.run_tool("plan-log", self.file("p.txt", text))
        self.assertEqual(code, 1)
        self.assertIn("FAIL plan-log: the plan was refused something (Forbidden)", lines)
        self.assertIn("FAIL plan-log: the plan has an error", lines)

    def test_an_error_in_a_boxed_terraform_output_counts(self):
        text = "╷\n│ Error: boom\n╵\n"
        code, lines = self.run_tool("plan-log", self.file("p.txt", text))
        self.assertEqual(code, 1)
        self.assertIn("errors: 1, forbidden mentions: 0, destroy or replace lines: 0", lines)

    def test_a_plan_that_never_finished_is_not_a_pass(self):
        code, lines = self.run_tool("plan-log", self.file("p.txt", "Refreshing state...\n"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL plan-log: no plan summary line (the plan did not finish)", lines)

    def test_a_destroy_or_replace_fails(self):
        for verb in ("will be destroyed", "must be replaced"):
            code, lines = self.run_tool("plan-log", self.file("p.txt", f"  # kubernetes_namespace_v1.worker[0] {verb}\n{SUMMARY}\n"))
            with self.subTest(verb):
                self.assertEqual(code, 1)
                self.assertIn("FAIL plan-log: the plan destroys or replaces an object", lines)

    def test_a_deployment_change_fails_because_it_restarts_the_manager(self):
        code, lines = self.run_tool("plan-log", self.file("p.txt", f"  # kubernetes_manifest.factory_deployment will be updated in-place\n{SUMMARY}\n"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL plan-log: the plan changes the manager Deployment (a restart on its ReadWriteOnce disk; pass --allow-deployment for the step that does)", lines)

    def test_a_deployment_change_passes_only_for_the_step_that_makes_it_and_is_still_counted_as_a_note(self):
        # E1 turns the pod backend on (slice 17 adds containers to the manager pod); nothing else may allow it, and it never allows a destroy.
        path = self.file("p.txt", f"  # kubernetes_manifest.factory_deployment will be updated in-place\n{SUMMARY}\n")
        code, lines = self.run_tool("plan-log", path, "--allow-deployment")
        self.assertEqual(code, 0, lines)
        self.assertIn("note: the plan changes the manager Deployment (1 lines), which this step allows", lines)
        replaced = self.file("r.txt", f"  # kubernetes_manifest.factory_deployment must be replaced\n{SUMMARY}\n")
        code, lines = self.run_tool("plan-log", replaced, "--allow-deployment")
        self.assertEqual(code, 1)
        self.assertIn("FAIL plan-log: the plan destroys or replaces an object", lines)
        both = self.file("b.txt", f"  # azurerm_kubernetes_cluster.this will be updated in-place\n  # kubernetes_manifest.factory_deployment will be updated in-place\n{SUMMARY}\n")
        self.assertEqual(self.run_tool("plan-log", both, "--allow-deployment")[0], 1)  # the cluster still needs its own flag

    def test_a_cluster_change_fails_unless_the_step_is_the_one_that_makes_it(self):
        path = self.file("p.txt", f"  # azurerm_kubernetes_cluster.this will be updated in-place\n{SUMMARY}\n")
        code, lines = self.run_tool("plan-log", path)
        self.assertEqual(code, 1)
        self.assertTrue(any("changes the cluster or a node pool" in line for line in lines))
        self.assertEqual(self.run_tool("plan-log", path, "--allow-cluster")[0], 0)

    def test_an_unreadable_file_and_a_huge_file_fail_closed(self):
        self.assertEqual(self.run_tool("plan-log", str(Path(self.directory.name, "missing")))[0], 1)
        big = self.file("big.txt", "x" * (rep.MAX_FILE_BYTES + 1))
        self.assertEqual(self.run_tool("plan-log", big)[1], ["FAIL plan-log: the file could not be read"])

    def test_it_prints_no_address_and_no_message_of_the_plan(self):
        text = CLEAN + '\nError: secret detail kubernetes_secret_v1.hidden\n'
        _, lines = self.run_tool("plan-log", self.file("p.txt", text))
        self.assertNotIn("hidden", "\n".join(lines))
        for line in lines:
            self.assertRegex(line, r"^(plan summary: |errors: |FAIL plan-log: |plan-log: )")


class BootCompare(Files):
    def nodes(self, **boots):
        return {"items": [{"metadata": {"name": name}, "status": {"nodeInfo": {"bootID": boot}}} for name, boot in boots.items()]}

    def test_a_restarted_a_gone_and_a_new_node_are_counted(self):
        before = self.file("b.json", self.nodes(a="1", b="2", c="3"))
        after = self.file("a.json", self.nodes(a="1", b="9", d="4"))
        code, lines = self.run_tool("boot-compare", before, after)
        self.assertEqual((code, lines), (0, ["nodes before: 3, after: 3; same boot: 1, restarted: 1, gone: 1, new: 1"]))

    def test_nodes_that_did_not_restart_are_all_the_same(self):
        same = self.file("s.json", self.nodes(a="1", b="2"))
        self.assertEqual(self.run_tool("boot-compare", same, same)[1], ["nodes before: 2, after: 2; same boot: 2, restarted: 0, gone: 0, new: 0"])

    def test_a_node_without_a_boot_id_or_a_file_that_is_not_a_node_list_fails_closed(self):
        good = self.file("g.json", self.nodes(a="1"))
        for content in ({"items": [{"metadata": {"name": "a"}, "status": {}}]}, "x", [1], {"items": [{"status": {"nodeInfo": {"bootID": "1"}}}]}):
            code, lines = self.run_tool("boot-compare", good, self.file("bad.json", content))
            with self.subTest(content=str(content)[:30]):
                self.assertEqual(code, 1)
                self.assertTrue(lines[0].startswith("FAIL boot-compare"))

    def test_no_node_name_is_printed(self):
        before = self.file("b.json", self.nodes(**{"aks-sentinel-1": "1"}))
        _, lines = self.run_tool("boot-compare", before, before)
        self.assertNotIn("sentinel", "\n".join(lines))


class Safety(unittest.TestCase):
    def test_it_reads_files_only(self):
        self.assertFalse(imported_modules("live_reports") & {"subprocess", "socket", "ssl", "http", "urllib", "requests", "shutil"})
        self.assertFalse(calls("live_reports") & {"os.system", "os.popen", "eval", "exec"})
        text = (rep.ROOT / "tools" / "live_reports.py").read_text()
        self.assertEqual(len(re.findall(r"open\(", text)), 1)
        self.assertIn('"rb"', text)  # read only


if __name__ == "__main__":
    unittest.main()
