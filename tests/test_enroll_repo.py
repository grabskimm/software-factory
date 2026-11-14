"""scripts/enroll-repo.sh, run against a fake ``gh`` that keeps a small GitHub in a JSON file.

The fake records every call (and what was piped to it), tells reads from writes the way GitHub
does (a method other than GET, a -f field, --input, ``repo edit``) and applies writes to its
state, so that ordering, idempotence and the ruleset payload can be checked for real. Nothing
here reaches the network; the real ``gh`` is never on the PATH.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "enroll-repo.sh"
HANDOFF = ROOT / "handoff"
WORKFLOW_PATH = ".github/workflows/factory-automerge.yml"
TOKEN = "ghp_TESTSECRETTOKEN0123456789abcdef"

FAKE_GH = r'''#!__PYTHON__
import fnmatch, json, os, subprocess, sys, urllib.parse

STATE = os.environ["FAKE_STATE"]
LOG = os.environ["FAKE_LOG"]
argv = sys.argv[1:]


def load():
    with open(STATE) as handle:
        return json.load(handle)


def save(state):
    with open(STATE, "w") as handle:
        json.dump(state, handle)


def fail(message):
    sys.stderr.write("gh: " + message + "\n")
    sys.exit(1)


def log(write, stdin):
    with open(LOG, "a") as handle:
        handle.write(json.dumps({"argv": argv, "write": write, "stdin": stdin}) + "\n")


def emit(data, jq, silent):
    if silent:
        return
    if jq:
        out = subprocess.run(["jq", "-r", jq], input=json.dumps(data), capture_output=True, text=True)
        if out.returncode:
            fail("jq: " + out.stderr)
        sys.stdout.write(out.stdout)
    else:
        sys.stdout.write(json.dumps(data) + "\n")


def matched(state, ruleset):
    name = state["repo"]["name"]
    cond = ruleset["conditions"].get("repository_name")
    if cond is None:
        return False
    inc = any(p == "~ALL" or fnmatch.fnmatchcase(name, p) for p in cond.get("include", []))
    exc = any(fnmatch.fnmatchcase(name, p) for p in cond.get("exclude", []))
    return inc and not exc


def main():
    state = load()
    if argv[:2] == ["repo", "edit"]:
        log(True, None)
        state["repo"]["allow_squash_merge"] = True
        state["repo"]["allow_auto_merge"] = True
        save(state)
        return
    if argv[:1] == ["auth"]:
        log(False, None)
        return
    if argv[0] != "api":
        fail("unsupported command " + " ".join(argv))
    method, jq, silent, include, fields, use_input, path = "GET", None, False, False, {}, False, None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-X":
            method = argv[i + 1]; i += 2
        elif a == "--jq":
            jq = argv[i + 1]; i += 2
        elif a == "-f":
            k, v = argv[i + 1].split("=", 1); fields[k] = v; i += 2
        elif a == "--input":
            use_input = True; i += 2
        elif a == "--silent":
            silent = True; i += 1
        elif a == "-i":
            include = True; i += 1
        elif a == "--paginate":
            i += 1
        else:
            path = a; i += 1
    stdin = sys.stdin.read() if use_input else None
    write = method != "GET" or bool(fields) or use_input
    if fields and method == "GET":
        method = "POST"
    log(write, stdin)
    path = path.lstrip("/")
    route, _, query = path.partition("?")
    q = urllib.parse.parse_qs(query)
    org, repo = state["org"], state["repo"]
    base = "repos/%s/%s" % (org, repo["name"])

    if state.get("down"):
        fail("HTTP 500")
    if route == "user":
        data = {"login": "owner-user", "email": "private@example.invalid"}
        if include:
            if state.get("scopes") is not None:
                sys.stdout.write("HTTP/2.0 200 OK\r\nX-Oauth-Scopes: %s\r\n\r\n" % state["scopes"])
            else:
                sys.stdout.write("HTTP/2.0 200 OK\r\n\r\n")
        emit(data, jq, silent); return
    if route == "user/memberships/orgs/" + org:
        emit({"role": state.get("role", "admin"), "state": "active"}, jq, silent); return
    if route == base and method == "GET":
        emit(repo, jq, silent); return
    if route == "orgs/%s/installations" % org:
        emit({"installations": state["installations"]}, jq, silent); return
    m = route.split("/")
    if route.startswith("user/installations/") and m[3:4] == ["repositories"]:
        inst = m[2]
        if method == "PUT":
            if state.get("install_put_fails"):
                fail("Resource not accessible by integration (HTTP 403)")
            state["installed"].setdefault(inst, []).append(int(m[4])); save(state); return
        emit({"repositories": [{"id": r} for r in state["installed"].get(inst, [])]}, jq, silent); return
    secret = "orgs/%s/actions/secrets/AUTOMERGE_APP_PRIVATE_KEY" % org
    if route == secret:
        if state.get("secret") is None:
            fail("Not Found (HTTP 404)")
        emit({"name": "AUTOMERGE_APP_PRIVATE_KEY", "visibility": state["secret"]["visibility"]}, jq, silent); return
    if route == secret + "/repositories":
        emit({"repositories": [{"id": r} for r in state["secret"]["repos"]]}, jq, silent); return
    if route.startswith(secret + "/repositories/") and method == "PUT":
        state["secret"]["repos"].append(int(m[-1])); save(state); return
    if route.startswith(base + "/contents/"):
        file_path = route[len(base + "/contents/"):]
        if method == "PUT":
            branch = fields["branch"]
            if branch not in state["files"]:
                fail("branch not found")
            state["files"][branch][file_path] = fields["content"]
            save(state); return
        ref = q.get("ref", [state["repo"]["default_branch"]])[0]
        content = state["files"].get(ref, {}).get(file_path)
        if content is None:
            fail("Not Found (HTTP 404)")
        wrapped = "\n".join(content[i:i + 60] for i in range(0, len(content), 60)) + "\n"
        emit({"sha": "blobsha", "content": wrapped}, jq, silent); return
    if route.startswith(base + "/git/ref/heads/"):
        name = route[len(base + "/git/ref/heads/"):]
        if name not in state["files"]:
            fail("Not Found (HTTP 404)")
        emit({"object": {"sha": "sha-of-" + name}}, jq, silent); return
    if route == base + "/git/refs" and method == "POST":
        name = fields["ref"][len("refs/heads/"):]
        state["files"][name] = dict(state["files"][repo["default_branch"]])
        save(state); return
    if route == base + "/pulls":
        if method == "POST":
            pr = {"html_url": "https://github.example/%s/%s/pull/%d" % (org, repo["name"], len(state["pulls"]) + 1),
                  "head": fields["head"], "base": fields["base"]}
            state["pulls"].append(pr); save(state); emit(pr, jq, silent); return
        emit([p for p in state["pulls"]], jq, silent); return
    if route == base + "/actions/permissions":
        emit({"enabled": state.get("actions_enabled", True)}, jq, silent); return
    if route == base + "/rules/branches/" + repo["default_branch"]:
        if state.get("rules_down"):
            fail("HTTP 502")
        out = []
        for rs in state["rulesets"]:
            if rs["enforcement"] == "active" and matched(state, rs):
                for rule in rs["rules"]:
                    if rule["type"] == "required_status_checks":
                        out.append(rule)
        emit(out, jq, silent); return
    if route.startswith(base + "/commits/") and route.endswith("/check-runs"):
        emit({"check_runs": [{"name": n} for n in state["check_runs"]]}, jq, silent); return
    if route.startswith(base + "/commits/") and route.endswith("/status"):
        emit({"statuses": []}, jq, silent); return
    if route == "orgs/%s/rulesets" % org:
        if method == "POST":
            body = json.loads(stdin)
            body["id"] = 900 + len(state["rulesets"])
            state["rulesets"].append(body); save(state); return
        emit([{"id": r["id"], "name": r["name"]} for r in state["rulesets"]], jq, silent); return
    if route.startswith("orgs/%s/rulesets/" % org):
        rid = int(m[-1])
        if state.get("rs_garbage") and method == "GET":
            sys.stdout.write("<html>oops\n"); return
        found = [r for r in state["rulesets"] if r["id"] == rid]
        if not found:
            fail("Not Found (HTTP 404)")
        if method == "PUT":
            body = json.loads(stdin)
            body["id"] = rid
            state["rulesets"] = [body if r["id"] == rid else r for r in state["rulesets"]]
            save(state); return
        emit(found[0], jq, silent); return
    fail("fake gh: unhandled %s %s" % (method, path))


main()
'''


def b64(text):
    return base64.b64encode(text.encode()).decode()


def base_state():
    return {
        "org": "acme",
        "scopes": "admin:org, repo, workflow",
        "role": "admin",
        "repo": {
            "id": 5150, "name": "newrepo", "full_name": "acme/newrepo", "owner": {"login": "acme"},
            "archived": False, "fork": False, "default_branch": "main", "visibility": "private",
            "allow_squash_merge": False, "allow_auto_merge": False,
        },
        "installations": [
            {"id": 111, "app_id": 4242, "app_slug": "factory-bot", "client_id": "Iv1.factoryapp",
             "repository_selection": "selected"},
            {"id": 222, "app_id": 999, "app_slug": "merge-app", "client_id": "Iv23limerge",
             "repository_selection": "selected"},
        ],
        "installed": {"111": [], "222": []},
        "secret": {"visibility": "selected", "repos": []},
        "files": {"main": {"README.md": b64("hello")}},
        "pulls": [],
        "check_runs": ["unit tests", "lint"],
        "rulesets": [{
            "id": 77, "name": "factory-handoff", "target": "branch", "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
                           "repository_name": {"include": ["other-repo"], "exclude": [], "protected": False}},
            "rules": [
                {"type": "deletion"},
                {"type": "required_status_checks", "parameters": {
                    "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                    "required_status_checks": [{"context": "lint"}, {"context": "unit tests"},
                                               {"context": "factory/gate", "integration_id": 4242}]}},
            ],
        }],
    }


ARGS = ["--org", "acme", "--repo", "newrepo", "--factory-app-slug", "factory-bot",
        "--automerge-client-id", "Iv23limerge"]
CI = ["--ci-check", "unit tests", "--ci-check", "lint"]


class EnrollBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="enroll-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH.replace("__PYTHON__", sys.executable))
        gh.chmod(0o755)
        self.state_file = self.tmp / "state.json"
        self.log_file = self.tmp / "calls.jsonl"
        self.log_file.write_text("")
        self.state = base_state()
        self.script = SCRIPT

    def save(self):
        self.state_file.write_text(json.dumps(self.state))

    def load(self):
        return json.loads(self.state_file.read_text())

    def run_script(self, *extra, apply=False):
        self.save()
        env = dict(os.environ)
        env.update({"PATH": "%s:%s" % (self.bin, env["PATH"]), "FAKE_STATE": str(self.state_file),
                    "FAKE_LOG": str(self.log_file), "GH_TOKEN": TOKEN, "ENROLL_RETRY_SLEEP": "0"})
        env.pop("GITHUB_TOKEN", None)
        cmd = ["bash", str(self.script)] + ARGS + list(extra) + (["--apply"] if apply else [])
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        self.state = self.load()
        return proc

    def calls(self):
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def writes(self):
        return [c for c in self.calls() if c["write"]]

    @staticmethod
    def describe(call):
        a = call["argv"]
        if a[:2] == ["repo", "edit"]:
            return "repo-edit"
        method = "GET"
        if "-X" in a:
            method = a[a.index("-X") + 1]
        path = next(x for x in a[1:] if "/" in x and not x.startswith("-") and x != "-")
        return "%s %s" % (method, path.split("?")[0])

    def merge_enrol_pr(self):
        self.state = self.load()
        self.state["files"]["main"].update(self.state["files"]["factory/enroll"])
        self.save()

    def out(self, proc):
        return proc.stdout + proc.stderr


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and shutil.which("base64"), "needs jq, bash and base64")
class DryRun(EnrollBase):
    def test_dry_run_makes_zero_write_calls(self):
        proc = self.run_script(*CI)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.load(), {**self.state})  # state untouched
        self.assertIn("DRY RUN", proc.stdout)
        self.assertIn("would run: gh api -X PUT user/installations/111/repositories/5150", proc.stdout)
        self.assertIn("would run: gh api -X PUT user/installations/222/repositories/5150", proc.stdout)
        self.assertIn("orgs/acme/actions/secrets/AUTOMERGE_APP_PRIVATE_KEY/repositories/5150", proc.stdout)
        self.assertIn("orgs/acme/rulesets/77", proc.stdout)
        self.assertIn('"newrepo"', proc.stdout)  # in the JSON the PUT would carry
        self.assertIn("installation_id = 111", proc.stdout)
        self.assertIn("uid = 10002", proc.stdout)
        self.assertIn("gid = 10010", proc.stdout)

    def test_dry_run_without_ci_check_prints_discovered_names_and_makes_no_ruleset_plan(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertIn("- unit tests", proc.stdout)
        self.assertIn("- lint", proc.stdout)
        self.assertIn("NOT used until you confirm", proc.stdout)
        self.assertNotIn("orgs/acme/rulesets/77", proc.stdout)

    def test_apply_without_ci_check_stops_with_12_before_any_write(self):
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 12, self.out(proc))
        self.assertEqual(self.writes(), [])


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and shutil.which("base64"), "needs jq, bash and base64")
class Apply(EnrollBase):
    def test_pr_mode_order_and_rerun_to_completion(self):
        first = self.run_script(*CI, apply=True)
        self.assertEqual(first.returncode, 11, self.out(first))
        self.assertIn("/pull/1", first.stdout)
        order = [self.describe(c) for c in self.writes()]
        self.assertEqual(order, [
            "repo-edit",
            "PUT user/installations/111/repositories/5150",
            "PUT user/installations/222/repositories/5150",
            "PUT orgs/acme/actions/secrets/AUTOMERGE_APP_PRIVATE_KEY/repositories/5150",
            "POST repos/acme/newrepo/git/refs",
            "PUT repos/acme/newrepo/contents/" + WORKFLOW_PATH,
            "POST repos/acme/newrepo/pulls",
        ])
        # the workflow is on the PR branch only, rendered with the slug and client id, and no ruleset change yet
        self.assertNotIn(WORKFLOW_PATH, self.state["files"]["main"])
        rendered = base64.b64decode(self.state["files"]["factory/enroll"][WORKFLOW_PATH]).decode()
        self.assertIn("'factory-bot[bot]'", rendered)
        self.assertIn('"Iv23limerge"', rendered)
        self.assertNotIn("<<", rendered)
        self.assertEqual(self.state["rulesets"][0]["conditions"]["repository_name"]["include"], ["other-repo"])

        # the PR is open and not merged: a re-run says so and still changes nothing
        self.log_file.write_text("")
        again = self.run_script(*CI, apply=True)
        self.assertEqual(again.returncode, 11, self.out(again))
        self.assertEqual(self.writes(), [])

        # after the PR is merged the run finishes; the workflow is read from the default branch before the ruleset is written
        self.merge_enrol_pr()
        self.log_file.write_text("")
        done = self.run_script(*CI, apply=True)
        self.assertEqual(done.returncode, 0, self.out(done))
        calls = self.calls()
        writes = [i for i, c in enumerate(calls) if c["write"]]
        self.assertEqual([self.describe(calls[i]) for i in writes], ["PUT orgs/acme/rulesets/77"])
        ruleset_put = writes[0]
        reads_of_workflow_on_main = [
            i for i, c in enumerate(calls[:ruleset_put])
            if WORKFLOW_PATH in " ".join(c["argv"]) and "ref=main" in " ".join(c["argv"])]
        self.assertTrue(reads_of_workflow_on_main, "workflow was not verified on main before the ruleset change")
        sent = json.loads(calls[ruleset_put]["stdin"])
        self.assertEqual(sent["conditions"]["repository_name"]["include"], ["other-repo", "newrepo"])
        self.assertEqual(sent["bypass_actors"], [])
        self.assertIn("verified: factory/gate is required from App id 4242", done.stdout)
        self.assertIn("installation_id = 111", done.stdout)

        # and a further run is a complete no-op
        self.log_file.write_text("")
        noop = self.run_script(*CI, apply=True)
        self.assertEqual(noop.returncode, 0, self.out(noop))
        self.assertEqual(self.writes(), [])
        self.assertTrue(self.state["repo"]["allow_auto_merge"])

    def test_direct_mode_commits_to_default_branch_before_the_ruleset(self):
        proc = self.run_script(*CI, "--workflow-mode", "direct", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        order = [self.describe(c) for c in self.writes()]
        self.assertEqual(order[-2:], ["PUT repos/acme/newrepo/contents/" + WORKFLOW_PATH, "PUT orgs/acme/rulesets/77"])
        self.assertNotIn("POST repos/acme/newrepo/pulls", order)
        self.assertIn(WORKFLOW_PATH, self.state["files"]["main"])
        self.log_file.write_text("")
        again = self.run_script(*CI, "--workflow-mode", "direct", apply=True)
        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertEqual(self.writes(), [])

    def test_repository_already_held_by_the_ruleset_without_workflow_is_refused_in_both_modes(self):
        self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = ["new*"]
        for mode in ("pr", "direct"):
            proc = self.run_script(*CI, "--workflow-mode", mode, apply=True)
            self.assertEqual(proc.returncode, 4, self.out(proc))
            self.assertIn("already held", proc.stderr)
        self.assertEqual([self.describe(c) for c in self.writes() if "rulesets" in " ".join(c["argv"])], [])
        self.assertNotIn(WORKFLOW_PATH, self.load()["files"]["main"])

    def test_workflow_that_exists_with_other_content_is_kept(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("name: hand edited\n")
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("differs from the rendered template", proc.stderr)
        self.assertEqual(base64.b64decode(self.state["files"]["main"][WORKFLOW_PATH]).decode(), "name: hand edited\n")

    def test_workflow_with_placeholder_on_default_branch_is_refused(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("user: '<<GITHUB_APP_SLUG>>[bot]'\n")
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertEqual([c for c in self.writes() if "rulesets" in " ".join(c["argv"])], [])

    def test_a_ci_check_that_starts_with_a_dash_is_data_not_a_jq_option(self):
        for value in ("-rf", "--foo"):
            self.log_file.write_text("")
            proc = self.run_script("--ci-check", value, apply=True)
            self.assertEqual(proc.returncode, 4, (value, self.out(proc)))  # a ruleset mismatch, not jq's own status 2
            self.assertEqual(self.writes(), [], value)
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        proc = self.run_script("--ci-check", "-rf", "--allow-unseen-check", "--new-ruleset", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        post = next(c for c in self.writes() if c["argv"][:3] == ["api", "-X", "POST"])
        checks = next(r for r in json.loads(post["stdin"])["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(checks["parameters"]["required_status_checks"][0], {"context": "-rf"})

    def test_unreadable_active_rules_do_not_look_like_no_rules(self):
        self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = ["new*"]
        self.state["rules_down"] = True
        for mode in ("pr", "direct"):
            proc = self.run_script(*CI, "--workflow-mode", mode, apply=True)
            self.assertEqual(proc.returncode, 1, self.out(proc))
            self.assertIn("cannot read the active rules", proc.stderr)
            self.assertFalse([c for c in self.writes() if WORKFLOW_PATH in " ".join(c["argv"])])
        self.assertNotIn(WORKFLOW_PATH, self.load()["files"]["main"])

    def test_a_new_ruleset_does_not_require_a_check_name_nothing_has_reported(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        for apply, code in ((True, 12), (False, 0)):
            self.log_file.write_text("")
            proc = self.run_script("--ci-check", "unit-tests-TYPO", "--new-ruleset", apply=apply)
            self.assertEqual(proc.returncode, code, self.out(proc))
            self.assertEqual(self.writes(), [])
        self.assertIn("--allow-unseen-check", self.out(proc))
        self.assertEqual(len(self.state["rulesets"]), 1)

    def test_another_ruleset_adding_a_check_fails_the_verification(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        self.state["rulesets"].append({
            "id": 88, "name": "org-wide", "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"repository_name": {"include": ["~ALL"], "exclude": []}},
            "rules": [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "ghost-check"}]}}]})
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 5, self.out(proc))
        self.assertIn("ghost-check", proc.stderr)

    def test_every_ruleset_refusal_comes_before_any_write(self):
        def excluded(state):
            state["rulesets"][0]["conditions"]["repository_name"]["exclude"] = ["newrepo"]

        def pinned_elsewhere(state):
            state["rulesets"][0]["rules"][1]["parameters"]["required_status_checks"][2]["integration_id"] = 1

        def other_selector(state):
            state["rulesets"][0]["conditions"] = {"repository_id": {"repository_ids": [1]}}

        cases = [(["--ci-check", "build"], lambda state: None), (list(CI), excluded), (list(CI), pinned_elsewhere),
                 (list(CI), other_selector)]
        for extra, mutate in cases:
            self.state = base_state()
            mutate(self.state)
            self.log_file.write_text("")
            proc = self.run_script(*extra, apply=True)
            self.assertEqual(proc.returncode, 4, (extra, self.out(proc)))
            self.assertEqual(self.writes(), [], (extra, self.out(proc)))

    def test_check_names_that_differ_are_refused_and_nothing_is_written_to_the_ruleset(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        proc = self.run_script("--ci-check", "build", apply=True)
        self.assertEqual(proc.returncode, 4, self.out(proc))
        self.assertIn("--new-ruleset", proc.stderr)
        self.assertEqual([c for c in self.writes() if "rulesets" in " ".join(c["argv"])], [])
        self.assertEqual(self.state["rulesets"][0]["conditions"]["repository_name"]["include"], ["other-repo"])

    def test_repository_already_matched_by_a_pattern_is_a_noop(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = ["new*"]
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("already matches", proc.stdout)
        self.assertEqual([c for c in self.writes() if "rulesets" in " ".join(c["argv"])], [])

    def test_excluded_repository_is_refused(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        self.state["rulesets"][0]["conditions"]["repository_name"]["exclude"] = ["newrepo"]
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 4, self.out(proc))

    def test_ruleset_gate_pinned_to_another_app_is_refused(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        self.state["rulesets"][0]["rules"][1]["parameters"]["required_status_checks"][2]["integration_id"] = 1
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 4, self.out(proc))

    def test_new_ruleset_payload_has_no_bypass_actors_and_pins_the_factory_app(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        proc = self.run_script("--ci-check", "build (py3.12)", "--allow-unseen-check", "--new-ruleset", "--ruleset", "handoff-group-b", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        posts = [c for c in self.writes() if c["argv"][:3] == ["api", "-X", "POST"] and "rulesets" in " ".join(c["argv"])]
        self.assertEqual(len(posts), 1)
        payload = json.loads(posts[0]["stdin"])
        self.assertEqual(payload["name"], "handoff-group-b")
        self.assertEqual(payload["bypass_actors"], [])
        self.assertEqual(payload["enforcement"], "active")
        self.assertEqual(payload["conditions"]["repository_name"]["include"], ["newrepo"])
        checks = next(r for r in payload["rules"] if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]
        self.assertEqual(checks, [{"context": "build (py3.12)"}, {"context": "factory/gate", "integration_id": 4242}])
        self.assertIsInstance(checks[1]["integration_id"], int)
        self.assertNotIn("<<", posts[0]["stdin"])
        # the other ruleset is untouched, and a re-run creates nothing
        self.assertEqual(self.state["rulesets"][0]["conditions"]["repository_name"]["include"], ["other-repo"])
        self.log_file.write_text("")
        again = self.run_script("--ci-check", "build (py3.12)", "--allow-unseen-check", "--new-ruleset", "--ruleset", "handoff-group-b", apply=True)
        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertEqual(self.writes(), [])

    def test_new_ruleset_dry_run_shows_the_payload_and_writes_nothing(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        proc = self.run_script("--no-ci", "--new-ruleset")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertIn("factory-handoff-newrepo", proc.stdout)
        self.assertIn('"bypass_actors": []', proc.stdout)

    def test_existing_ruleset_name_without_new_ruleset_flag_must_exist(self):
        proc = self.run_script(*CI, "--ruleset", "nope", apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertEqual(self.writes(), [])

    def test_manual_step_exit_code_and_continue_flag(self):
        self.state["install_put_fails"] = True
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 10, self.out(proc))
        self.assertIn("https://github.com/organizations/acme/settings/installations/111", proc.stdout)
        self.assertIn("https://github.com/organizations/acme/settings/installations/222", proc.stdout)
        # nothing after step 2 happened
        self.assertNotIn("POST repos/acme/newrepo/git/refs", [self.describe(c) for c in self.writes()])
        self.log_file.write_text("")
        cont = self.run_script(*CI, "--continue-after-manual", apply=True)
        self.assertEqual(cont.returncode, 11, self.out(cont))  # carried on as far as the enrolment PR
        self.assertIn("POST repos/acme/newrepo/pulls", [self.describe(c) for c in self.writes()])

    def test_installed_apps_and_selected_secret_are_not_written_again(self):
        self.state["installed"] = {"111": [5150], "222": [5150]}
        self.state["secret"]["repos"] = [5150]
        self.state["repo"]["allow_squash_merge"] = True
        self.state["repo"]["allow_auto_merge"] = True
        self.state["installations"][0]["repository_selection"] = "selected"
        proc = self.run_script(*CI, "--workflow-mode", "direct", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        names = [self.describe(c) for c in self.writes()]
        self.assertEqual(names, ["PUT repos/acme/newrepo/contents/" + WORKFLOW_PATH, "PUT orgs/acme/rulesets/77"])

    def test_verification_failure_exit_code(self):
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        self.state["rulesets"][0]["enforcement"] = "evaluate"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 5, self.out(proc))


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and shutil.which("base64"), "needs jq, bash and base64")
class Refusals(EnrollBase):
    def test_repositories_that_cannot_run_the_workflow_are_refused_before_any_write(self):
        cases = {
            "empty": lambda s: s.update(files={}),
            "fork": lambda s: s["repo"].update(fork=True),
            "actions disabled": lambda s: s.update(actions_enabled=False),
        }
        for name, mutate in cases.items():
            self.state = base_state()
            mutate(self.state)
            self.log_file.write_text("")
            proc = self.run_script(*CI, apply=True)
            self.assertEqual(proc.returncode, 3, (name, self.out(proc)))
            self.assertEqual(self.writes(), [], name)

    def test_a_ruleset_answer_that_is_not_json_is_refused_before_any_write(self):
        self.state["rs_garbage"] = True
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("not a JSON object", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_organisation_secret_is_checked_before_any_write(self):
        self.state["secret"] = None
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("Not Found", proc.stderr)  # the real reason, not a guess
        self.assertEqual(self.writes(), [])

    def test_private_visibility_secret_covers_a_private_repository_only(self):
        self.state["secret"] = {"visibility": "private", "repos": []}
        self.state["files"]["main"][WORKFLOW_PATH] = b64("x")
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertNotIn("actions/secrets", " ".join(" ".join(c["argv"]) for c in self.writes()))
        self.state["repo"]["visibility"] = "public"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))

    def test_archived_repository(self):
        self.state["repo"]["archived"] = True
        for apply in (False, True):
            proc = self.run_script(*CI, apply=apply)
            self.assertEqual(proc.returncode, 3, self.out(proc))
            self.assertIn("archived", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_repository_of_another_organisation(self):
        self.state["repo"]["owner"]["login"] = "elsewhere"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertEqual(self.writes(), [])

    def test_missing_admin_org_scope(self):
        self.state["scopes"] = "repo, workflow"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("admin:org", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_missing_workflow_scope_when_the_workflow_must_be_created(self):
        self.state["scopes"] = "repo, admin:org"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("workflow", proc.stderr)

    def test_not_an_org_owner(self):
        self.state["role"] = "member"
        proc = self.run_script(*CI, apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertEqual(self.writes(), [])

    def test_missing_apps_and_secret(self):
        self.state["installations"] = self.state["installations"][:1]
        proc = self.run_script(*CI)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("merge-enrolment App", proc.stderr)
        self.state = base_state()
        self.state["secret"] = None
        proc = self.run_script(*CI)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("AUTOMERGE_APP_PRIVATE_KEY", proc.stderr)

    def test_usage_errors(self):
        for extra in (["--workflow-mode", "sideways"], ["--ci-check", ""], ["--no-ci", "--ci-check", "x"], ["--bogus"],
                      ["--ci-check", "factory/gate"], ["--repo", "..a"]):
            proc = self.run_script(*extra)
            self.assertEqual(proc.returncode, 2, (extra, self.out(proc)))
        self.assertEqual(self.calls(), [])

    def test_placeholder_guard_refuses_before_any_gh_call(self):
        tree = self.tmp / "tree"
        (tree / "scripts").mkdir(parents=True)
        (tree / "handoff").mkdir()
        shutil.copy(SCRIPT, tree / "scripts" / "enroll-repo.sh")
        shutil.copy(HANDOFF / "ruleset.json", tree / "handoff" / "ruleset.json")
        text = (HANDOFF / "factory-automerge.yml").read_text()
        (tree / "handoff" / "factory-automerge.yml").write_text(text + "# <<UNEXPECTED_PLACEHOLDER>>\n")
        self.script = tree / "scripts" / "enroll-repo.sh"
        for apply in (False, True):
            proc = self.run_script(*CI, apply=apply)
            self.assertNotEqual(proc.returncode, 0, self.out(proc))
            self.assertIn("'<<'", proc.stderr)
        self.assertEqual(self.calls(), [])


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and shutil.which("base64"), "needs jq, bash and base64")
class NoSecrets(EnrollBase):
    def test_token_and_secret_endpoints(self):
        runs = [self.run_script(*CI), self.run_script(*CI, apply=True), self.run_script(*CI, "--new-ruleset", apply=True)]
        for proc in runs:
            self.assertNotIn(TOKEN, self.out(proc))
            self.assertNotIn("ghp_", self.out(proc))
            self.assertNotIn("private@example.invalid", self.out(proc))
        for call in self.calls():
            line = " ".join(call["argv"]) + (call["stdin"] or "")
            self.assertNotIn(TOKEN, line)
            self.assertNotIn("public-key", line)
            self.assertNotIn("--hostname", line)
        # the secret is only ever addressed by metadata and by repository membership
        for call in self.calls():
            for arg in call["argv"]:
                if "actions/secrets" in arg:
                    self.assertIn(arg.split("AUTOMERGE_APP_PRIVATE_KEY")[1], ("", "/repositories", "/repositories/5150"))


class Static(unittest.TestCase):
    def test_syntax(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_executable_and_strict(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        text = SCRIPT.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -Eeuo pipefail", text)
        self.assertIn("trap 'exit 1' ERR", text)

    def test_forbidden_constructs_are_absent_from_the_code(self):
        code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("eval ", "--admin", "--force", "push -f", "-X DELETE", "git push", "pr merge", "--delete", "git branch -D",
                          "bypass_actors = [{", "permission-workflows"):
            self.assertNotIn(forbidden, code)
        # the only writes go through do_write
        self.assertEqual(code.count("gh repo edit"), 1)
        for line in code.splitlines():
            if "gh api -X" in line and "do_write" not in line:
                # continuation lines of a do_write call or a printed hint only
                self.assertTrue(line.strip().startswith(("gh api -X", "echo", "say", "#")), line)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        proc = subprocess.run(["shellcheck", "-x", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    unittest.main()
