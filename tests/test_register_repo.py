"""scripts/register-repo.sh (manual-merge registration), run against a fake ``gh`` and a fake ``curl``.

The fake ``gh`` keeps a small GitHub in a JSON file: the organisation's App installations and
rulesets, the target repository, the Factory repository's Actions variables. It records every
call (and what was piped to it, and the mode of the file on its stdin), tells reads from writes
the way GitHub does and applies writes to its state, so ordering, idempotence, the lost-update
guard and the written variable can be checked for real. Nothing here reaches the network; the
real ``gh`` and ``curl`` are never on the PATH.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "register-repo.sh"
HELPER = ROOT / "scripts" / "tfvars_repo_configs.py"
TOKEN = "ghp_TESTSECRETTOKEN0123456789abcdef"
API_TOKEN = "mgr.TOKEN-0123456789abcdefXYZ"

FAKE_GH = r'''#!__PYTHON__
import fnmatch, json, os, stat, subprocess, sys

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


def log(write, stdin=None, mode=None):
    with open(LOG, "a") as handle:
        handle.write(json.dumps({"argv": argv, "write": write, "stdin": stdin, "stdin_mode": mode}) + "\n")


def emit(data, jq, silent):
    if silent:
        return
    if jq:
        out = subprocess.run(["jq", "-r", jq], input=json.dumps(data).encode(), capture_output=True)
        if out.returncode:
            fail("jq: " + out.stderr.decode())
        sys.stdout.flush()
        sys.stdout.buffer.write(out.stdout)
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
    if argv[:2] == ["repo", "view"]:
        log(False)
        if state.get("cwd_repo") is None:
            fail("no git remotes found")
        emit({"nameWithOwner": state["cwd_repo"]}, argv[argv.index("--jq") + 1] if "--jq" in argv else None, False)
        return
    if argv[:2] == ["variable", "list"]:
        log(False)
        repo = argv[argv.index("-R") + 1]
        if state.get("variables_down"):
            fail("HTTP 403: Resource not accessible")
        if repo != state["factory_repo"]["full"]:
            fail("Not Found (HTTP 404)")
        state["reads"] = state.get("reads", 0) + 1
        mut = state.get("mutate_on_read")
        if mut and state["reads"] == mut["nth"]:
            state["variables"]["INFRA_TFVARS"] += mut["append"]
        save(state)
        data = [{"name": k, "value": v} for k, v in state["variables"].items()]
        emit(data, argv[argv.index("--jq") + 1], False)
        return
    if argv[:2] == ["variable", "set"]:
        data = sys.stdin.buffer.read().decode()
        log(True, data, stat.S_IMODE(os.fstat(0).st_mode))
        if state.get("set_fails"):
            fail("HTTP 403")
        # gh trims trailing CR/LF of a value read from stdin
        state["variables"][argv[2]] = data.rstrip("\r\n")
        if state.get("set_corrupts"):
            state["variables"][argv[2]] += "\n<<BROKEN\n"
        if state.get("down_after_set"):
            state["variables_down"] = True
        save(state)
        sys.stdout.write("Updated variable\n")
        return
    if argv[:1] == ["workflow"] or argv[:2] == ["pr", "merge"]:
        log(True)
        fail("the script must never run this")
    if argv[0] != "api":
        log(True)
        fail("unsupported command " + " ".join(argv))
    method, jq, silent, include, path = "GET", None, False, False, None
    fields, use_input = {}, False
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
    write = method != "GET" or bool(fields) or use_input
    log(write)
    path = path.lstrip("/")
    route = path.split("?")[0]
    org, repo = state["org"], state["repo"]
    base = "repos/%s/%s" % (org, repo["name"])
    if route.lower().startswith(base.lower()):  # GitHub's repository routes are case-insensitive
        route = base + route[len(base):]
    if state.get("down"):
        fail("HTTP 500")
    if route == "user":
        if include:
            if state.get("unauthenticated"):
                fail("To get started with GitHub CLI, please run:  gh auth login")
            if state.get("scopes") is not None:
                sys.stdout.write("HTTP/2.0 200 OK\r\nX-Oauth-Scopes: %s\r\n\r\n" % state["scopes"])
            else:
                sys.stdout.write("HTTP/2.0 200 OK\r\n\r\n")
        emit({"login": "owner-user", "email": "private@example.invalid"}, jq, silent); return
    fac = state["factory_repo"]
    if route == "repos/" + fac["full"]:
        if fac.get("missing"):
            fail("Not Found (HTTP 404)")
        emit({"default_branch": fac["default_branch"], "archived": fac.get("archived", False)}, jq, silent); return
    if route == base and method == "GET":
        if state.get("repo_missing"):
            fail("Not Found (HTTP 404)")
        emit(repo, jq, silent); return
    if route == "orgs/%s/installations" % org:
        emit({"installations": state["installations"]}, jq, silent); return
    parts = route.split("/")
    if route.startswith("user/installations/") and parts[3:4] == ["repositories"]:
        inst = parts[2]
        if method == "PUT":
            if state.get("install_put_fails"):
                fail("Resource not accessible by integration (HTTP 403)")
            state["installed"].setdefault(inst, []).append(int(parts[4])); save(state); return
        if state.get("unlistable"):
            fail("HTTP 403")
        emit({"repositories": [{"id": r} for r in state["installed"].get(inst, [])]}, jq, silent); return
    if route.startswith(base + "/git/ref/heads/"):
        name = route[len(base + "/git/ref/heads/"):]
        if state.get("empty_repo") or name != repo["default_branch"]:
            fail("Not Found (HTTP 404)")
        emit({"object": {"sha": "abc"}}, jq, silent); return
    if route == base + "/rules/branches/" + repo["default_branch"]:
        if state.get("rules_down"):
            fail("HTTP 502")
        out = []
        for rs in state["rulesets"]:
            if rs["enforcement"] == "active" and matched(state, rs):
                out.extend(r for r in rs["rules"] if r["type"] == "required_status_checks")
        emit(out, jq, silent); return
    if route == "orgs/%s/rulesets" % org:
        if state.get("rulesets_down"):
            fail("HTTP 403")
        emit([{"id": r["id"], "name": r["name"]} for r in state["rulesets"]], jq, silent); return
    if route.startswith("orgs/%s/rulesets/" % org):
        found = [r for r in state["rulesets"] if r["id"] == int(parts[-1])]
        if not found:
            fail("Not Found (HTTP 404)")
        emit(found[0], jq, silent); return
    fail("fake gh: unhandled %s %s" % (method, path))


main()
'''

FAKE_CURL = r'''#!__PYTHON__
import json, os, sys

argv = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["FAKE_CURL_LOG"], "a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
out = argv[argv.index("--output") + 1]
with open(os.environ["FAKE_CURL_BODY"]) as src, open(out, "w") as dst:
    dst.write(src.read())
sys.stdout.write(os.environ.get("FAKE_CURL_CODE", "200"))
'''

TFVARS = '''# Terraform inputs of the factory (test fixture)
subscription_id = "SUBSCRIPTION-MARKER-0000"
tenant_id       = "TENANT-MARKER-1111"

tags = {
  "owner" = "team # not a comment"
}

# One entry per repository; the key is `org/name`.
repo_configs = {
  "acme/existing" = <<-TOML
    repo            = "acme/existing"
    base_branch     = "main"
    installation_id = 111

    [worker]
    uid = 10002
    gid = 10010

    [sandbox]
    verify_domains = ["pypi.org"]
  TOML
}

# --- Sizing
aks_sku_tier = "Free"
'''


def base_state():
    return {
        "org": "acme",
        "scopes": "admin:org, repo",
        "cwd_repo": "acme/factory",
        "factory_repo": {"full": "acme/factory", "default_branch": "main"},
        "repo": {"id": 5150, "name": "newrepo", "full_name": "acme/newrepo", "owner": {"login": "acme"},
                 "archived": False, "fork": False, "default_branch": "main", "visibility": "private"},
        "installations": [
            {"id": 111, "app_id": 4242, "app_slug": "factory-bot", "repository_selection": "selected"},
            {"id": 222, "app_id": 999, "app_slug": "merge-app", "repository_selection": "selected"},
        ],
        "installed": {"111": [5150], "222": []},
        "variables": {"INFRA_TFVARS": TFVARS, "OTHER_VARIABLE": "OTHER-VARIABLE-MARKER"},
        "rulesets": [{
            "id": 77, "name": "factory-handoff", "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
                           "repository_name": {"include": ["other-repo"], "exclude": [], "protected": False}},
            "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "factory/gate", "integration_id": 4242}]}}],
        }],
    }


ARGS = ["--org", "acme", "--repo", "newrepo", "--factory-app-slug", "factory-bot"]


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash") and shutil.which("python3"), "needs jq, bash and python3")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="register-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL)):
            exe = self.bin / name
            exe.write_text(body.replace("__PYTHON__", sys.executable))
            exe.chmod(0o755)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.state_file = self.tmp / "state.json"
        self.log_file = self.tmp / "calls.jsonl"
        self.log_file.write_text("")
        self.curl_log = self.tmp / "curl.jsonl"
        self.curl_log.write_text("")
        self.curl_body = self.tmp / "curl-body.json"
        self.curl_body.write_text(json.dumps({"repos": []}))
        self.state = base_state()
        self.script = SCRIPT

    def save(self):
        self.state_file.write_text(json.dumps(self.state))

    def run_script(self, *extra, apply=False, env_extra=None, args=ARGS):
        self.save()
        env = dict(os.environ)
        env.update({"PATH": "%s:%s" % (self.bin, env["PATH"]), "FAKE_STATE": str(self.state_file),
                    "FAKE_LOG": str(self.log_file), "GH_TOKEN": TOKEN, "TMPDIR": str(self.scratch),
                    "FAKE_CURL_LOG": str(self.curl_log), "FAKE_CURL_BODY": str(self.curl_body)})
        for name in ("GITHUB_TOKEN", "FACTORY_API_TOKEN"):
            env.pop(name, None)
        env.update(env_extra or {})
        cmd = ["bash", str(self.script)] + list(args) + list(extra) + (["--apply"] if apply else [])
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        self.state = json.loads(self.state_file.read_text())
        return proc

    def calls(self):
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def writes(self):
        return [c for c in self.calls() if c["write"]]

    @staticmethod
    def describe(call):
        a = call["argv"]
        if a[:2] in (["variable", "set"], ["variable", "list"]):
            return " ".join(a[:3])
        method = a[a.index("-X") + 1] if "-X" in a else "GET"
        path = next(x for x in a[1:] if "/" in x and not x.startswith("-"))
        return "%s %s" % (method, path.split("?")[0])

    @staticmethod
    def out(proc):
        return proc.stdout + proc.stderr

    def variable(self):
        return self.state["variables"]["INFRA_TFVARS"]


class DryRun(Base):
    def test_dry_run_makes_zero_write_calls_and_shows_the_plan(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.variable(), TFVARS)
        out = proc.stdout
        self.assertIn("DRY RUN", out)
        self.assertIn('+  "acme/newrepo" = <<-TOML', out)
        self.assertIn('+    repo            = "acme/newrepo"', out)
        self.assertIn('+    base_branch     = "main"', out)
        self.assertIn("+    installation_id = 111", out)
        self.assertIn('+    abandon_after   = "7d"', out)
        self.assertIn('+    auto_resume_after_restart = "once"', out)
        self.assertIn("+    uid = 10002", out)
        self.assertIn("+    gid = 10010", out)
        self.assertIn('+    verify_domains = ["pypi.org", "files.pythonhosted.org"]', out)
        self.assertIn('+    probe_url = "https://pypi.org/"', out)
        self.assertNotIn("run_budget_usd", out)
        self.assertIn("would run: gh variable set INFRA_TFVARS -R acme/factory", out)
        # the first read is the only variable read in a dry run, and the App is already installed
        self.assertEqual([self.describe(c) for c in self.calls() if c["argv"][:1] == ["variable"]], ["variable list -R"])
        self.assertEqual(os.listdir(self.scratch), [])  # the temporary directory is gone

    def test_dry_run_prints_only_the_repo_configs_region(self):
        proc = self.run_script()
        for marker in ("SUBSCRIPTION-MARKER", "TENANT-MARKER", "OTHER-VARIABLE-MARKER", "aks_sku_tier", "not a comment"):
            self.assertNotIn(marker, self.out(proc))

    def test_remaining_steps_are_printed_and_nothing_is_dispatched(self):
        proc = self.run_script()
        out = proc.stdout
        self.assertIn("GET  <<MANAGER_URL>>/repos", out)
        self.assertIn("POST <<MANAGER_URL>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>/drain", out)
        # two lines that can be pasted as they are: the plan, then the apply
        self.assertIn("(b) Apply the Terraform of infra/ with the new INFRA_TFVARS value", out)
        self.assertIn("      from the checkout of acme/factory on main:\n        terraform -chdir=infra plan\n        terraform -chdir=infra apply\n", out)
        self.assertNotIn("terraform -chdir=infra plan,", out)
        self.assertEqual(out.count("terraform -chdir=infra"), 2)
        self.assertIn("Read the plan", out)
        self.assertIn('GET <<MANAGER_URL>>/repos lists "acme/newrepo" with no "error" field', out)
        applied = self.run_script(apply=True)
        self.assertEqual(applied.returncode, 0, self.out(applied))
        self.assertFalse([c for c in self.calls() if c["argv"][:1] == ["workflow"]])

    def test_factory_repo_defaults_to_the_current_checkout_and_the_option_wins(self):
        proc = self.run_script()
        self.assertIn("Factory repository (from the current checkout): acme/factory", proc.stdout)
        self.assertIn(["repo", "view"], [c["argv"][:2] for c in self.calls()])
        self.log_file.write_text("")
        self.state["factory_repo"] = {"full": "acme/infra-box", "default_branch": "trunk"}
        proc = self.run_script("--factory-repo", "acme/infra-box")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertNotIn(["repo", "view"], [c["argv"][:2] for c in self.calls()])
        self.assertIn("      from the checkout of acme/infra-box on trunk:\n        terraform -chdir=infra plan\n        terraform -chdir=infra apply\n", proc.stdout)

    def test_entry_options(self):
        proc = self.run_script("--run-budget", "12.5", "--verify-domain", "PyPI.org", "--verify-domain", "files.pythonhosted.org",
                               "--verify-domain", "pypi.org", "--probe-url", "https://pypi.org/simple/")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("+    run_budget_usd  = 12.5", proc.stdout)
        self.assertIn('+    verify_domains = ["pypi.org", "files.pythonhosted.org"]', proc.stdout)
        self.assertIn('+    probe_url = "https://pypi.org/simple/"', proc.stdout)
        proc = self.run_script("--verify-domain", "files.pythonhosted.org")
        self.assertIn('+    probe_url = "https://files.pythonhosted.org/"', proc.stdout)

    def test_base_branch_and_installation_are_read_not_assumed(self):
        self.state["repo"]["default_branch"] = "develop"
        self.state["installations"][0]["id"] = 31337
        self.state["installed"] = {"31337": [5150]}
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn('+    base_branch     = "develop"', proc.stdout)
        self.assertIn("+    installation_id = 31337", proc.stdout)


class AutoBuild(Base):
    """Watching is the default, so a default registration writes no auto_build key; the options write one."""

    def test_a_default_registration_writes_no_auto_build_key(self):
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertNotIn("auto_build", self.variable())
        self.assertNotIn("auto_build", proc.stdout.split("== Step 3")[0].split("== Step 2")[1])
        self.assertIn("the manager now watches acme/newrepo", proc.stdout)
        self.assertIn("'factory-build'", proc.stdout)

    def test_no_auto_build_writes_false_and_says_so(self):
        proc = self.run_script("--no-auto-build", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("auto_build      = false", self.variable())
        self.assertIn("auto_build = false", proc.stdout)
        self.assertIn("by hand", proc.stdout)

    def test_the_options_are_written_with_their_keys(self):
        proc = self.run_script(
            "--auto-build-label", "ship it", "--auto-build-concurrency", "4", "--auto-build-max-issues", "2",
            "--auto-build-min-permission", "maintain", apply=True,
        )
        self.assertEqual(proc.returncode, 0, self.out(proc))
        text = self.variable()
        for line in ('auto_build_label = "ship it"', 'auto_build_min_permission = "maintain"',
                     "auto_build_concurrency = 4", "auto_build_max_issues = 2"):
            self.assertIn(line, text)
        self.assertNotIn("auto_build ", text.replace("auto_build_", ""))  # no auto_build = false

    def test_the_written_keys_load_as_a_valid_repo_config(self):
        sys.path.insert(0, str(ROOT / "src"))
        self.addCleanup(sys.path.remove, str(ROOT / "src"))
        from factory.config import load_repo_config

        proc = self.run_script("--no-auto-build", "--auto-build-label", "ship it", "--auto-build-concurrency", "4",
                               "--auto-build-max-issues", "2", "--auto-build-min-permission", "admin", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        body = self.variable().split('"acme/newrepo" = <<-TOML\n')[1].split("  TOML")[0]
        config = self.tmp / "config"
        (config / "repos").mkdir(parents=True)
        (config / "repos" / "acme__newrepo.toml").write_text("\n".join(line.strip() for line in body.splitlines()) + "\n")
        cfg = load_repo_config("acme/newrepo", config_dir=config)
        self.assertEqual(
            (cfg.auto_build, cfg.auto_build_label, cfg.auto_build_concurrency, cfg.auto_build_max_issues,
             cfg.auto_build_min_permission),
            (False, "ship it", 4, 2, "admin"),
        )

    def test_a_permission_below_write_is_refused_unless_the_repository_is_private(self):
        proc = self.run_script("--auto-build-min-permission", "triage")  # the fixture repository is private
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn('auto_build_min_permission = "triage"', proc.stdout)
        for level in ("read", "triage"):
            self.state["repo"]["visibility"] = "public"
            proc = self.run_script("--auto-build-min-permission", level, apply=True)
            self.assertEqual(proc.returncode, 3, self.out(proc))
            self.assertIn("only allowed for a private repository", proc.stderr)
            self.assertEqual(self.writes(), [])
        for level in ("write", "maintain", "admin"):
            proc = self.run_script("--auto-build-min-permission", level)
            self.assertEqual(proc.returncode, 0, self.out(proc))

    def test_an_existing_entry_is_compared_on_the_auto_build_options_you_pass(self):
        self.assertEqual(self.run_script("--no-auto-build", "--auto-build-max-issues", "2", apply=True).returncode, 0)
        registered = self.variable()
        self.log_file.write_text("")
        proc = self.run_script(apply=True)  # nothing passed: not compared
        self.assertIn("NO-OP", proc.stdout)
        self.assertIn("auto_build", proc.stdout)
        self.assertEqual(self.writes(), [])
        proc = self.run_script("--auto-build-max-issues", "5", apply=True)
        self.assertIn("DIFFERENT", proc.stdout)
        self.assertIn("auto_build_max_issues", proc.stdout)
        proc = self.run_script("--auto-build-max-issues", "2", "--no-auto-build", apply=True)
        self.assertIn("NO-OP", proc.stdout)
        self.assertEqual(self.variable(), registered)
        # Watching is wanted again: --replace-config compares the auto_build keys too and drops them.
        proc = self.run_script("--replace-config", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertNotIn("auto_build", self.variable())


class Apply(Base):
    def test_apply_writes_the_variable_and_only_the_entry_changes(self):
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
        new = self.variable()
        marker = '  "acme/newrepo" = <<-TOML\n'
        self.assertIn(marker, new)
        start = new.index(marker)
        end = new.index("  TOML\n", start) + len("  TOML\n")
        self.assertEqual(new[:start] + new[end:], TFVARS.rstrip("\n"))  # everything else is byte for byte the same
        self.assertTrue(new.index("acme/existing") < start)
        self.assertTrue(new[end:].startswith("}\n"), "the entry sits right before the closing brace")
        self.assertIn("verified: INFRA_TFVARS now has the entry for acme/newrepo", proc.stdout)
        call = self.writes()[0]
        self.assertEqual(call["argv"], ["variable", "set", "INFRA_TFVARS", "-R", "acme/factory"])
        self.assertEqual(call["stdin_mode"] & 0o077, 0, "the temporary file must not be readable by others")
        self.assertEqual(call["stdin"].rstrip("\r\n"), new)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_order_app_then_reread_then_write(self):
        self.state["installed"] = {"111": [], "222": []}
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        names = [self.describe(c) for c in self.calls() if c["argv"][0] in ("variable", "api")
                 and (c["argv"][0] == "variable" or c["write"])]
        self.assertEqual(names, [
            "variable list -R",
            "PUT user/installations/111/repositories/5150",
            "variable list -R",
            "variable set INFRA_TFVARS",
            "variable list -R",
        ])
        self.assertEqual(self.state["installed"]["111"], [5150])

    def test_a_second_run_is_a_clean_noop(self):
        self.assertEqual(self.run_script(apply=True).returncode, 0)
        self.log_file.write_text("")
        again = self.run_script(apply=True)
        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertEqual(self.writes(), [])
        self.assertIn("NO-OP: acme/newrepo is registered in manual mode", again.stdout)
        self.assertIn("identical", again.stdout)
        dry = self.run_script()
        self.assertEqual(dry.returncode, 0)
        self.assertIn("NO-OP", dry.stdout)
        self.assertNotIn("DRY-RUN  set", dry.stdout)

    def test_lost_update_guard(self):
        self.state["mutate_on_read"] = {"nth": 2, "append": "\n# edited by someone else\n"}
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 1, self.out(proc))
        self.assertIn("changed since it was read", proc.stderr)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("acme/newrepo", self.variable())
        self.assertIn("edited by someone else", self.variable())
        self.assertEqual(os.listdir(self.scratch), [])

    def test_an_edit_before_the_first_read_is_simply_planned_against(self):
        self.state["variables"]["INFRA_TFVARS"] = TFVARS + "# newer\n"
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("# newer", self.variable())

    def test_write_failure_is_reported(self):
        self.state["set_fails"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 1, self.out(proc))
        self.assertIn("could not write the variable INFRA_TFVARS", proc.stderr)

    def test_existing_identical_entry_is_a_noop_even_when_indented_differently(self):
        entry = ('  "acme/newrepo" = <<-TOML\n        repo            = "acme/newrepo"\n        base_branch     = "main"\n'
                 '        installation_id = 111\n        abandon_after   = "7d"\n        auto_resume_after_restart = "once"\n\n'
                 '        [worker]\n        uid = 10002\n        gid = 10010\n\n        [sandbox]\n'
                 '        verify_domains = ["pypi.org", "files.pythonhosted.org"]\n\n        [preflight]\n'
                 '        probe_url = "https://pypi.org/"\n  TOML\n')
        self.state["variables"]["INFRA_TFVARS"] = TFVARS.replace("}\n\n# ---", entry + "}\n\n# ---")
        before = self.variable()
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.variable(), before)
        self.assertIn("identical entry", proc.stdout)

    def test_existing_different_entry_is_reported_and_kept_until_replace_config(self):
        self.assertEqual(self.run_script(apply=True).returncode, 0)
        registered = self.variable()
        self.log_file.write_text("")
        proc = self.run_script("--run-budget", "30", "--verify-domain", "files.pythonhosted.org", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.variable(), registered)
        out = proc.stdout
        self.assertIn("DIFFERENT content", out)
        self.assertIn("NOT overwritten", out)
        self.assertIn("+run_budget_usd  = 30", out)
        self.assertIn('-verify_domains = ["pypi.org", "files.pythonhosted.org"]', out)
        self.assertIn("--replace-config", out)
        self.assertNotIn("acme/existing", out)  # the diff covers this entry only
        self.assertNotIn("SUBSCRIPTION-MARKER", out)
        self.assertNotIn("NO-OP", out)

        replaced = self.run_script("--run-budget", "30", "--verify-domain", "files.pythonhosted.org", "--replace-config", apply=True)
        self.assertEqual(replaced.returncode, 0, self.out(replaced))
        self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
        new = self.variable()
        self.assertIn("run_budget_usd  = 30", new)
        self.assertEqual(new.count('"acme/newrepo" = <<-TOML'), 1)
        self.assertIn('"acme/existing" = <<-TOML', new)
        self.assertTrue(new.endswith('aks_sku_tier = "Free"'))
        self.assertTrue(new.startswith(TFVARS[:200]))
        self.log_file.write_text("")
        again = self.run_script("--run-budget", "30", "--verify-domain", "files.pythonhosted.org", "--replace-config", apply=True)
        self.assertEqual(self.writes(), [])
        self.assertIn("NO-OP", again.stdout)

    def test_replace_config_dry_run_shows_the_change_and_writes_nothing(self):
        self.assertEqual(self.run_script(apply=True).returncode, 0)
        self.log_file.write_text("")
        proc = self.run_script("--run-budget", "30", "--replace-config")
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertIn("+    run_budget_usd  = 30", proc.stdout)
        self.assertIn("would run: gh variable set INFRA_TFVARS", proc.stdout)

    def test_the_48kb_limit(self):
        pad = "# " + "x" * 78 + "\n"
        entry_room = 600
        text = TFVARS
        while len(text.encode()) < 49152 - entry_room - len(pad):
            text += pad
        self.state["variables"]["INFRA_TFVARS"] = text
        fits = self.run_script()
        self.assertEqual(fits.returncode, 0, self.out(fits))
        self.state["variables"]["INFRA_TFVARS"] = text + pad * 12
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("49152", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_crlf_variable_keeps_its_line_endings(self):
        crlf = TFVARS.replace("\n", "\r\n")
        self.state["variables"]["INFRA_TFVARS"] = crlf
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        new = self.variable()
        self.assertEqual(new.count("\n"), new.count("\r\n"))
        self.assertIn('"acme/newrepo" = <<-TOML\r\n', new)


class Refusals(Base):
    def refused(self, code, text=None, **kw):
        proc = self.run_script(apply=True, **kw)
        self.assertEqual(proc.returncode, code, self.out(proc))
        self.assertEqual(self.writes(), [])
        if text:
            self.assertIn(text, proc.stderr)
        return proc

    def test_repository_matched_by_the_handoff_ruleset_is_refused_with_4(self):
        self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = ["other-repo", "newrepo"]
        proc = self.refused(4, "manual mode is meaningless")
        self.assertIn("scripts/enroll-repo.sh", proc.stderr)
        # also when only dry-running
        self.log_file.write_text("")
        self.assertEqual(self.run_script().returncode, 4)
        self.assertEqual(self.writes(), [])

    def test_ruleset_pattern_and_all_match_but_exclusion_wins(self):
        for include in (["new*"], ["~ALL"]):
            self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = include
            self.refused(4)
        self.state["rulesets"][0]["conditions"]["repository_name"]["exclude"] = ["newrepo"]
        self.assertEqual(self.run_script(apply=True).returncode, 0)

    def test_a_ruleset_name_option_is_used(self):
        self.state["rulesets"][0]["name"] = "other-name"
        self.state["rulesets"][0]["conditions"]["repository_name"]["include"] = ["newrepo"]
        # the default name does not exist: the active rules still show the gate on this repository
        self.refused(4, "already require factory/gate")
        self.assertEqual(self.run_script("--ruleset", "other-name", apply=True).returncode, 4)

    def test_other_repositories_ruleset_is_fine_and_disabled_one_is_not_consulted(self):
        self.assertEqual(self.run_script().returncode, 0)

    def test_unlistable_rulesets_are_refused_not_guessed(self):
        self.state["rulesets_down"] = True
        self.refused(3, "cannot list the rulesets")

    def test_unreadable_active_rules_only_warn(self):
        self.state["rules_down"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("could not read the active rules", proc.stderr)

    def test_app_not_installed(self):
        self.state["installations"] = self.state["installations"][1:]
        self.refused(3, "no installation of the Factory App")

    def test_repository_problems(self):
        for change, text in (({"archived": True}, "archived"), ({"fork": True}, "fork"),
                             ({"owner": {"login": "elsewhere"}}, "belongs to"), ({"default_branch": "ma$in"}, "characters"),
                             ({"default_branch": 'a"b'}, "characters"), ({"default_branch": "x${y}"}, "characters")):
            self.setUp()
            self.state["repo"].update(change)
            self.refused(3, text)
        self.setUp()
        self.state["empty_repo"] = True
        self.refused(3, "empty repository")
        self.setUp()
        self.state["repo_missing"] = True
        self.refused(3, "not found")

    def test_auth_and_scopes(self):
        self.state["unauthenticated"] = True
        self.refused(3, "not authenticated")
        self.setUp()
        self.state["scopes"] = "read:org"
        self.refused(3, "repo scope")
        self.setUp()
        self.state["scopes"] = None
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("scopes are not reported", proc.stderr)

    def test_factory_repository_problems(self):
        self.state["cwd_repo"] = None
        self.refused(3, "pass --factory-repo")
        self.setUp()
        self.state["factory_repo"]["missing"] = True
        self.refused(3, "not found or not accessible")
        self.setUp()
        self.state["factory_repo"]["archived"] = True
        self.refused(3, "archived")
        self.setUp()
        self.state["variables_down"] = True
        proc = self.refused(3, "cannot read the repository variables")
        self.assertNotIn("MARKER", self.out(proc))

    def test_variable_problems(self):
        del self.state["variables"]["INFRA_TFVARS"]
        self.refused(3, "does not exist")
        for text, message in (
            ('subscription_id = "x"\n', "exactly one"),
            (TFVARS + 'repo_configs = {}\n', "exactly one"),
            ('repo_configs = merge(local.a, local.b)\n', "literal"),
            ('repo_configs = {\n  "a/b" = <<-TOML\n  x = 1\n', "heredoc"),
            ('# repo_configs = {}\n', "exactly one"),
        ):
            self.setUp()
            self.state["variables"]["INFRA_TFVARS"] = text
            proc = self.refused(3, message)
            self.assertNotIn("subscription_id", self.out(proc))

    def test_app_not_covering_the_repository_stops_with_10(self):
        self.state["installed"] = {"111": [], "222": []}
        self.state["install_put_fails"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 10, self.out(proc))
        self.assertIn("https://github.com/organizations/acme/settings/installations/111", proc.stdout)
        self.assertIn("MANUAL STEP NEEDED", proc.stdout)
        self.assertEqual(self.writes(), [{"argv": ["api", "-X", "PUT", "user/installations/111/repositories/5150", "--silent"],
                                          "write": True, "stdin": None, "stdin_mode": None}])
        self.assertNotIn("acme/newrepo", self.variable())  # the variable is untouched
        self.assertEqual(os.listdir(self.scratch), [])
        # a dry run only announces it
        self.log_file.write_text("")
        dry = self.run_script()
        self.assertEqual(dry.returncode, 0, self.out(dry))
        self.assertEqual(self.writes(), [])
        self.assertIn("exit 10", dry.stdout + dry.stderr)
        self.assertIn("settings/installations/111", dry.stdout)

    def test_continue_after_manual(self):
        self.state["installed"] = {"111": [], "222": []}
        self.state["install_put_fails"] = True
        proc = self.run_script("--continue-after-manual", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("continuing", proc.stdout)
        self.assertIn("acme/newrepo", self.variable())

    def test_unlistable_installation_tries_once_and_falls_back_to_the_manual_step(self):
        self.state["unlistable"] = True
        self.state["install_put_fails"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 10, self.out(proc))
        self.assertIn("cannot list the repositories of the Factory App installation", proc.stdout)
        puts = [c for c in self.writes() if "-X" in c["argv"]]
        self.assertEqual(len(puts), 1)
        self.state["install_put_fails"] = False
        ok = self.run_script(apply=True)
        self.assertEqual(ok.returncode, 0, self.out(ok))

    def test_an_app_on_all_repositories_needs_no_call(self):
        self.state["installations"][0]["repository_selection"] = "all"
        self.state["installed"] = {"111": [], "222": []}
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
        self.assertIn("installed on all repositories", proc.stdout)


class WriteSafety(Base):
    """INFRA_TFVARS is verified locally before the write, and a failure after it is exit 5."""

    REPLACE_TEXTS = {
        "trailing comma": 'repo_configs = {\n  "acme/existing" = "x",\n  "acme/newrepo" = "old",\n}\n',
        "trailing comment": 'repo_configs = {\n  "acme/newrepo" = "old" # note\n}\n',
        "comma and next entry": 'repo_configs = {\n  "acme/newrepo" = "old", # note\n  "z/y" = "q"\n}\n',
        "one line map": 'repo_configs = { "acme/newrepo" = "old", "z/y" = "q" }\n',
    }

    def test_replace_config_next_to_a_comma_or_comment_writes_a_valid_variable(self):
        for name, text in self.REPLACE_TEXTS.items():
            with self.subTest(name):
                self.setUp()
                self.state["variables"]["INFRA_TFVARS"] = text
                proc = self.run_script("--replace-config", apply=True)
                self.assertEqual(proc.returncode, 0, self.out(proc))
                new = self.variable()
                self.assertNotIn("TOML,", new)
                for line in new.splitlines():
                    if line.strip().startswith("TOML"):
                        self.assertEqual(line.strip(), "TOML")
                self.assertIn("verified: INFRA_TFVARS now has the entry", proc.stdout)
                self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
                again = self.run_script("--replace-config", apply=True)
                self.assertIn("NO-OP", again.stdout)

    def test_a_text_that_does_not_verify_is_never_written(self):
        # a copy of the scripts whose editor has lost its self-check and renders a broken terminator
        copy = self.tmp / "copy"
        copy.mkdir()
        shutil.copy(SCRIPT, copy / "register-repo.sh")
        helper = (HELPER.read_text()
                  .replace("        self_check(text, new_text, key, wanted, others_before)\n", "        pass\n")
                  .replace("    self_check(text, new_text, key, wanted, others_before)\n", "    pass\n")
                  .replace("    out.append(\"%s%s\" % (indent, MARKER))", "    out.append(\"%s%s,\" % (indent, MARKER))"))
        self.assertNotEqual(helper, HELPER.read_text())
        (copy / "tfvars_repo_configs.py").write_text(helper)
        self.script = copy / "register-repo.sh"
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("unterminated heredoc", proc.stderr)  # found by reading the new text back, before the write
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.variable(), TFVARS)

    def test_the_editor_itself_refuses_a_bad_result_before_the_write(self):
        # the shipped helper cannot produce one; a refusal in it is a preflight refusal (3), never a write
        self.state["variables"]["INFRA_TFVARS"] = 'repo_configs = {\n  "acme/newrepo" = "old"\n  "acme/NEWREPO" = "again"\n}\n'
        proc = self.run_script("--replace-config", apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertEqual(self.writes(), [])

    def test_a_variable_that_is_broken_after_the_write_is_exit_5_with_fix_it_by_hand(self):
        self.state["set_corrupts"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 5, self.out(proc))
        self.assertIn("written but does not verify", proc.stderr)
        self.assertIn("fix it by hand", proc.stderr)
        self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
        self.assertEqual(os.listdir(self.scratch), [])

    def test_a_variable_that_cannot_be_read_after_the_write_is_exit_5(self):
        self.state["down_after_set"] = True
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 5, self.out(proc))
        self.assertIn("fix it by hand", proc.stderr)

    def test_failures_before_the_write_keep_their_codes(self):
        self.state["variables_down"] = True
        self.assertEqual(self.run_script(apply=True).returncode, 3)
        self.setUp()
        self.state["mutate_on_read"] = {"nth": 2, "append": "\n# x\n"}
        self.assertEqual(self.run_script(apply=True).returncode, 1)


class RegionOnly(Base):
    def test_comments_on_the_brace_lines_are_never_printed(self):
        text = TFVARS.replace("repo_configs = {", "repo_configs = { # ghp_FAKE_OPEN").replace("\n}\n\n# ---", "\n} # ghp_FAKE_CLOSE\n\n# ---")
        self.assertIn("ghp_FAKE_CLOSE", text)
        self.state["variables"]["INFRA_TFVARS"] = text
        for kwargs in ({}, {"apply": True}):
            proc = self.run_script(**kwargs)
            self.assertEqual(proc.returncode, 0, self.out(proc))
            self.assertNotIn("ghp_FAKE", self.out(proc))
            self.assertIn('+  "acme/newrepo" = <<-TOML', proc.stdout)
        self.assertIn("ghp_FAKE_CLOSE", self.variable())  # the variable keeps them


class Spelling(Base):
    def test_the_key_uses_the_spelling_github_reports(self):
        proc = self.run_script(apply=True, args=["--org", "acme", "--repo", "NewRepo", "--factory-app-slug", "factory-bot"])
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("GitHub spells the repository acme/newrepo", proc.stdout)
        self.assertIn('"acme/newrepo" = <<-TOML', self.variable())
        self.assertNotIn("NewRepo", self.variable())
        again = self.run_script(apply=True, args=["--org", "acme", "--repo", "NEWREPO", "--factory-app-slug", "factory-bot"])
        self.assertIn("NO-OP", again.stdout)
        self.assertEqual(self.variable().count("newrepo\" = <<-TOML"), 1)

    def test_a_renamed_repository_is_refused(self):
        self.state["repo"]["full_name"] = "acme/renamed"
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 3, self.out(proc))
        self.assertIn("acme/renamed", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_default_branch_with_a_slash_reaches_the_ref_call_unencoded(self):
        self.state["repo"]["default_branch"] = "feature/a-b_c"
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn('base_branch     = "feature/a-b_c"', proc.stdout)
        refs = [c["argv"][1] for c in self.calls() if len(c["argv"]) > 1 and "/git/ref/" in c["argv"][1]]
        self.assertEqual(refs, ["repos/acme/newrepo/git/ref/heads/feature/a-b_c"])


class ExistingEntry(Base):
    OPTIONS = ["--run-budget", "20", "--verify-domain", "pypi.org", "--verify-domain", "files.pythonhosted.org",
               "--verify-domain", "registry.npmjs.org"]

    def test_defaults_do_not_make_a_hand_registered_entry_differ(self):
        self.assertEqual(self.run_script(*self.OPTIONS, apply=True).returncode, 0)
        registered = self.variable()
        self.assertIn("run_budget_usd  = 20", registered)
        self.log_file.write_text("")
        for kwargs in ({}, {"apply": True}):
            proc = self.run_script(**kwargs)
            self.assertEqual(proc.returncode, 0, self.out(proc))
            self.assertEqual(self.writes(), [])
            self.assertIn("NO-OP", proc.stdout)
            self.assertIn("identical entry", proc.stdout)
            self.assertIn("not compared", proc.stdout)
            self.assertIn("run_budget_usd", proc.stdout)
            self.assertNotIn("DIFFERENT", proc.stdout)
        self.assertEqual(self.variable(), registered)

    def test_a_passed_field_is_compared(self):
        self.assertEqual(self.run_script(*self.OPTIONS, apply=True).returncode, 0)
        for extra, shown in ((["--run-budget", "30"], "run_budget_usd"), (["--verify-domain", "pypi.org"], "verify_domains"),
                             (["--probe-url", "https://pypi.org/simple/"], "probe_url")):
            proc = self.run_script(*extra, apply=True)
            self.assertEqual(proc.returncode, 0, self.out(proc))
            self.assertIn("DIFFERENT", proc.stdout)
            self.assertIn(shown, proc.stdout)
            self.assertNotIn("NO-OP", proc.stdout)

    def test_a_different_entry_without_replace_config_is_exit_0_and_says_so(self):
        self.state["variables"]["INFRA_TFVARS"] = TFVARS.replace("}\n\n# ---", '  "acme/newrepo" = "old"\n}\n\n# ---')
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual(self.writes(), [])
        self.assertIn("NOT overwritten", proc.stdout)
        self.assertIn("exit 0, nothing written", proc.stdout)
        self.assertNotIn("NO-OP", proc.stdout)


class AssumeInstalled(Base):
    """A gh OAuth token cannot list the installation's repositories, and cannot add either (403)."""

    def setUp(self):
        super().setUp()
        self.state["unlistable"] = True
        self.state["install_put_fails"] = True
        self.state["installed"] = {"111": [], "222": []}

    def register(self):
        self.assertEqual(self.run_script("--assume-installed", apply=True).returncode, 0)
        self.log_file.write_text("")

    def test_without_the_option_the_run_stops_with_10_and_names_the_option(self):
        proc = self.run_script(apply=True)
        self.assertEqual(proc.returncode, 10, self.out(proc))
        self.assertIn("--assume-installed", proc.stdout)

    def test_with_the_option_no_api_call_is_made_and_the_rerun_reaches_the_noop(self):
        self.register()
        again = self.run_script("--assume-installed", apply=True)
        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertEqual(self.writes(), [])
        self.assertFalse([c for c in self.calls() if "-X" in c["argv"]])
        self.assertIn("NO-OP", again.stdout)
        self.assertIn("ASSUMED", again.stdout)
        dry = self.run_script("--assume-installed")
        self.assertEqual(dry.returncode, 0, self.out(dry))
        self.assertIn("NO-OP", dry.stdout)
        self.assertNotIn("would run: gh api", dry.stdout)
        self.assertNotIn("exit 10", dry.stdout)

    def test_first_registration_with_the_option_writes_the_variable_only(self):
        proc = self.run_script("--assume-installed", apply=True)
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertEqual([self.describe(c) for c in self.writes()], ["variable set INFRA_TFVARS"])
        self.assertIn("NOT proven", proc.stdout)

    def test_continue_after_manual_also_reaches_the_noop_but_says_assumed(self):
        self.register()
        again = self.run_script("--continue-after-manual", apply=True)
        self.assertEqual(again.returncode, 0, self.out(again))
        self.assertIn("NO-OP", again.stdout)
        self.assertIn("ASSUMED", again.stdout)

    def test_the_option_does_not_override_a_listing_that_says_the_repository_is_missing(self):
        self.state["unlistable"] = False
        proc = self.run_script("--assume-installed", apply=True)
        self.assertEqual(proc.returncode, 10, self.out(proc))
        self.assertIn("does not list", proc.stdout)


class Validation(Base):
    def rejected(self, *extra, args=ARGS, env_extra=None):
        proc = self.run_script(*extra, args=args, env_extra=env_extra)
        self.assertEqual(proc.returncode, 2, "%r -> %s" % (extra or args, self.out(proc)))
        self.assertEqual(self.calls(), [], "no gh call may happen before the values are validated")
        return proc

    def replaced(self, option, value):
        args = list(ARGS)
        args[args.index(option) + 1] = value
        return args

    def test_org_repo_slug(self):
        for bad in ('a"b', "a b", "a\nb", "-x", "x-", "a$(id)", "a;b", "", "--", "a.b", "a/b"):
            self.rejected(args=self.replaced("--org", bad))
        for bad in ('x"y', "x\ny", "..", "a..b", ".", "x.", "$(id)", "a/b", "--x y", "x y", "", "x;y", "x'y", "x.git", "x{y}", "x`id`"):
            self.rejected(args=self.replaced("--repo", bad))
        for bad in ("bad[bot]", "a b", 'x"', "", "a\nb", "--", "-a"):
            self.rejected(args=self.replaced("--factory-app-slug", bad))

    def test_factory_repo_and_ruleset(self):
        for bad in ("acme", "acme/fac tory", "a/b/c", "--x/y", "acme/..", 'acme/x"', "acme/", "/x", "a\nb/c", "acme/$(id)", "host/acme/factory"):
            self.rejected("--factory-repo", bad)
        for bad in ("", "a\nb", "a\tb"):
            self.rejected("--ruleset", bad)

    def test_verify_domain(self):
        for bad in ("pypi.org.", '"pypi.org"', 'pypi.org"; x = "', "pypi.org\nfoo", "*.pypi.org", "https://pypi.org", "pypi.org:443",
                    "<<TOML", "a$(id).org", "${x}.org", "%{x}.org", "localhost", "1.2.3.4", "--", "-pypi.org", "pypi..org",
                    " ", "", "pypi.org/x", "py_pi.org", "pypi.org\\", "pypi.org'", "pypi.org#", "a" * 64 + ".org",
                    "x." * 130 + "org", ".pypi.org", "pypi.-org.com"):
            self.rejected("--verify-domain", bad)

    def test_probe_url(self):
        for bad in ("http://pypi.org/", "https://evil.com/", "https://pypi.org/a b", 'https://pypi.org/"', "https://u@pypi.org/",
                    "https://pypi.org:8443/", "https://pypi.org/?q=1", "https://pypi.org/<<TOML", "https://pypi.org/${x}",
                    "https://pypi.org/\nTOML", "https://pypi.org/#f", "pypi.org", "https://", "https://pypi.org.evil.com/",
                    "https://pypi.org./", "--", "https://pypi.org/%{x}", "https://pypi.org/'", "https://pypi.org/\\", "https://pypi.org/$(id)"):
            self.rejected("--probe-url", bad)

    def test_run_budget(self):
        for bad in ("0", "0.0", "-5", "abc", "1e3", "5; rm", '"5"', "5\n", "NaN", "inf", "1,5", "01", "", " 5", "5 ", "1.", ".5", "9999999999", "--", "$(id)"):
            self.rejected("--run-budget", bad)

    def test_auto_build_options(self):
        for bad in ("", " x", "x ", "factory-ignore", "Factory-Ignore", "factory:blocked", 'a"b', "a\nb", "a,b", "x" * 51,
                    "-x", "a$(id)", "ü", "--"):
            self.rejected("--auto-build-label", bad)
        for bad in ("0", "65", "-1", "x", "1.5", "", "01", "1e1", "99999"):
            self.rejected("--auto-build-concurrency", bad)
        for bad in ("0", "51", "-1", "x", "", "007", "1000"):
            self.rejected("--auto-build-max-issues", bad)
        for bad in ("none", "owner", "Write", "", "write ", "admin\n", "--"):
            self.rejected("--auto-build-min-permission", bad)
        self.rejected("--auto-build-label")  # a value is required
        proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        for option in ("--no-auto-build", "--auto-build-label", "--auto-build-concurrency", "--auto-build-max-issues",
                       "--auto-build-min-permission"):
            self.assertIn(option, proc.stdout)
        self.assertNotIn("--auto-build ", proc.stdout.replace("--auto-build-", ""))  # there is no opt-in flag: it is the default

    def test_verify_url_and_token(self):
        good = {"FACTORY_API_TOKEN": API_TOKEN}
        for bad in ("ftp://x", "http://evil.com", 'https://x"', "https://x y", "https://x/\nz", "x", "--", "https://x/$(id)", "https://u@x/"):
            self.rejected("--verify-url", bad, env_extra=good)
        proc = self.rejected("--verify-url", "https://manager.example")
        self.assertIn("FACTORY_API_TOKEN", proc.stderr)
        for token in ('tok"en', "tok en", "tok\nen", "tok$(id)en", "tok;en"):
            proc = self.rejected("--verify-url", "https://manager.example", env_extra={"FACTORY_API_TOKEN": token})
            self.assertNotIn(token, self.out(proc))
        self.assertEqual(self.curl_log.read_text(), "")

    def test_usage(self):
        self.rejected(args=["--org", "acme"])
        self.rejected("--bogus")
        self.rejected("--org")
        proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("usage:", proc.stdout)

    def test_a_value_starting_with_dashes_is_data_not_an_option(self):
        proc = self.rejected(args=["--org", "--repo", "--repo", "newrepo", "--factory-app-slug", "factory-bot"])
        self.assertIn("invalid --org", proc.stderr)


class VerifyUrl(Base):
    def verify(self, repos, code="200", **kw):
        self.curl_body.write_text(json.dumps({"repos": repos}))
        return self.run_script("--verify-url", "https://manager.example/", env_extra={"FACTORY_API_TOKEN": API_TOKEN, "FAKE_CURL_CODE": code}, **kw)

    def register(self):
        self.assertEqual(self.run_script(apply=True).returncode, 0)
        self.log_file.write_text("")

    def test_listed(self):
        self.register()
        proc = self.verify([{"repo": "acme/newrepo", "base_branch": "main", "supervisor": None}])
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("listed: the manager lists acme/newrepo with no error field", proc.stdout)
        self.assertIn("NO-OP: acme/newrepo is registered in manual mode and the manager lists it", proc.stdout)
        self.assertEqual(self.writes(), [])

    def test_listed_with_error(self):
        self.register()
        proc = self.verify([{"repo": "acme/newrepo", "error": "invalid configuration"}])
        self.assertEqual(proc.returncode, 5, self.out(proc))
        self.assertIn("listed with error", proc.stderr)

    def test_absent_after_the_entry_was_added_is_a_failure_but_before_it_is_expected(self):
        self.register()
        proc = self.verify([{"repo": "acme/other"}])
        self.assertEqual(proc.returncode, 5, self.out(proc))
        self.assertIn("absent", proc.stderr)
        self.setUp()
        proc = self.verify([{"repo": "acme/other"}])  # entry not yet in the variable
        self.assertEqual(proc.returncode, 0, self.out(proc))
        self.assertIn("absent: the manager does not list acme/newrepo yet", proc.stdout)
        self.assertEqual(self.writes(), [])  # a dry run

    def test_token_only_via_curl_config_on_stdin_and_never_printed(self):
        self.register()
        proc = self.verify([{"repo": "acme/newrepo"}])
        calls = [json.loads(line) for line in self.curl_log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertNotIn(API_TOKEN, " ".join(calls[0]["argv"]))
        self.assertIn("--config", calls[0]["argv"])
        self.assertEqual(calls[0]["stdin"], 'header = "Authorization: Bearer %s"\n' % API_TOKEN)
        self.assertEqual(calls[0]["argv"][calls[0]["argv"].index("--url") + 1], "https://manager.example/repos")
        self.assertNotIn(API_TOKEN, self.out(proc))
        for call in self.calls():
            self.assertNotIn(API_TOKEN, json.dumps(call))

    def test_failures_to_answer(self):
        self.register()
        proc = self.verify([], code="503")
        self.assertEqual(proc.returncode, 1, self.out(proc))
        self.assertIn("HTTP 503", proc.stderr)
        self.curl_body.write_text("<html>")
        proc = self.run_script("--verify-url", "https://manager.example", env_extra={"FACTORY_API_TOKEN": API_TOKEN})
        self.assertEqual(proc.returncode, 1, self.out(proc))

    def test_no_curl_call_without_the_option(self):
        self.run_script(env_extra={"FACTORY_API_TOKEN": API_TOKEN})
        self.assertEqual(self.curl_log.read_text(), "")


class NoSecrets(Base):
    def test_token_and_other_variables_never_appear(self):
        runs = [self.run_script(), self.run_script(apply=True), self.run_script("--replace-config", apply=True),
                self.run_script("--run-budget", "5", apply=True)]
        for proc in runs:
            text = self.out(proc)
            for secret in (TOKEN, "ghp_", "private@example.invalid", "OTHER-VARIABLE-MARKER", "SUBSCRIPTION-MARKER", "TENANT-MARKER"):
                self.assertNotIn(secret, text)
        for call in self.calls():
            line = " ".join(call["argv"])
            self.assertNotIn(TOKEN, line)
            self.assertNotIn("--hostname", line)
        # the variable is only ever addressed by name, with the exact filter
        for call in self.calls():
            if call["argv"][:1] == ["variable"]:
                self.assertIn(call["argv"][1], ("list", "set"))
        listing = next(c for c in self.calls() if c["argv"][:2] == ["variable", "list"])
        self.assertIn('.[]|select(.name=="INFRA_TFVARS")|.value', listing["argv"])


class Static(unittest.TestCase):
    def test_syntax_and_modes(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertTrue(os.access(HELPER, os.X_OK))
        text = SCRIPT.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -Eeuo pipefail", text)
        self.assertIn("trap 'exit 1' ERR", text)
        self.assertIn("umask 077", text)
        self.assertTrue(HELPER.read_text().startswith("#!/usr/bin/env python3\n"))
        compiled = subprocess.run([sys.executable, "-m", "py_compile", str(HELPER)], capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_forbidden_constructs_are_absent_from_the_code(self):
        code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("eval ", "--admin", "--force", "push -f", "-X DELETE", "git push", "pr merge", "--delete", "gh variable list -R \"$FACTORY_REPO\" --json name,value\n",
                          "gh workflow run -R", "gh run", "kubectl", "az "):
            self.assertNotIn(forbidden, code)
        # the only writes go through do_write
        self.assertEqual(code.count("gh variable set"), 1)
        self.assertEqual(code.count("gh api -X"), 1)
        for line in code.splitlines():
            if "gh variable set" in line or "gh api -X" in line:
                self.assertIn("do_write", line)
        # gh workflow run exists only inside a printf that shows the operator what to type
        for line in code.splitlines():
            if "gh workflow run" in line:
                self.assertTrue(line.lstrip().startswith("printf "), line)
        # the token never goes into an argument list
        for line in code.splitlines():
            if "FACTORY_API_TOKEN" in line and "curl" in line:
                self.fail(line)
        self.assertIn("--config -", code)
        self.assertIn("mktemp -d", code)

    def test_tracked_files_are_executable_in_the_git_index(self):
        # core.fileMode may be false (a /mnt/c checkout): then only the index knows. Untracked
        # files are the committer's business (git add --chmod=+x); once tracked they must be 100755.
        if not shutil.which("git"):
            self.skipTest("git not installed")
        for path in (SCRIPT, HELPER):
            proc = subprocess.run(["git", "ls-files", "-s", "--", str(path)], cwd=ROOT, capture_output=True, text=True)
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            self.assertTrue(proc.stdout.startswith("100755 "), "%s must be committed with mode 100755: %s" % (path.name, proc.stdout))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        proc = subprocess.run(["shellcheck", "-x", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    unittest.main()
