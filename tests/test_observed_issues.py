"""Issues Factory opens for what a worker observed (src/factory/observed_issues.py), with a fake ``gh``.

The fake keeps the repository's issues in $FAKE_DIR/issues.json (created ones included), the labels of the
source issues in labels.json and their states in states.json, and logs every call to calls.jsonl.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import cli, issue_comments, issue_labels, observed_issues, planner, schemas
from factory.config import RepoConfig
from factory.issue_labels import IssueLabels, drop_unadopted, is_unadopted
from factory.locks import LockLost
from factory.observed_issues import (
    ObservedIssues,
    clean_observation,
    fingerprint,
    merge,
    parse_observations,
    render,
    stored,
    valid_path,
)
from factory.state import State

from tests import schemacheck

SECRET = "SECRET-ISSUE-TEXT-do-not-print"
TOKEN = "ghp_0123456789abcdefghijklmnop"

FAKE_GH = r"""
import json, os, pathlib, re, sys, urllib.parse
root = pathlib.Path(os.environ['FAKE_DIR'])
args = sys.argv[1:]
with open(root / 'calls.jsonl', 'a') as log:
    log.write(f'{json.dumps(args)}\n')
def load(name, default):
    store = root / name
    return json.loads(store.read_text()) if store.exists() else default
def save(name, data):
    (root / name).write_text(json.dumps(data))
path = next((a for a in args[1:] if a.startswith('repos/')), '')
listing = re.fullmatch(r'repos/[^/]+/[^/]+/issues\?(.*)', path)
creating = re.fullmatch(r'repos/[^/]+/[^/]+/issues', path) and args[1:3] == ['-X', 'POST']
single = re.fullmatch(r'repos/[^/]+/[^/]+/issues/(\d+)', path)
login = os.environ.get('FAKE_LOGIN', 'factory-bot[bot]')
mode = os.environ.get('FAKE_OBS_FAIL')
if listing and mode == '403':
    sys.stderr.write(f'HTTP 403 {os.environ["FAKE_SECRET"]}')
    sys.exit(1)
if listing and mode == 'garbage':
    print(f'this is not json {os.environ["FAKE_SECRET"]}')
    sys.exit(0)
if listing and mode == 'timeout':
    import time
    time.sleep(60)
if creating and os.environ.get('FAKE_OBS_POST_FAIL'):
    sys.stderr.write(f'HTTP 403 {os.environ["FAKE_SECRET"]}')
    sys.exit(1)
if single and os.environ.get('FAKE_ISSUE_READ_FAIL'):
    sys.stderr.write(f'HTTP 500 {os.environ["FAKE_SECRET"]}')
    sys.exit(1)
if args == ['api', 'user', '--jq', '.login']:
    print(os.environ.get('FAKE_WHOAMI', login))
elif args[:1] == ['api'] and listing and '--paginate' in args:
    query = urllib.parse.parse_qs(listing.group(1))
    jq = args[args.index('--jq') + 1]
    by_login = re.search(r'\.user\.login == "(.*?)"', jq)
    by_id = re.search(r'\.user\.id == (\d+)', jq)
    for item in load('issues.json', []):
        if 'all' != query.get('state', ['open'])[0] and item['state'] != query['state'][0]:
            continue
        if query['labels'][0] not in item['labels']:
            continue
        if by_login and item['user'].get('login') != by_login.group(1):
            continue
        if by_id and (item['user'].get('id') != int(by_id.group(1)) or item['user'].get('type') != 'Bot'):
            continue
        if item.get('pull_request') is not None:
            continue
        found = re.match(r'<!-- factory-observed:v1 fp=([0-9a-f]{32}) -->', item['body'] or '')
        if found:
            print(json.dumps(found.group(1)))
elif creating:
    fields = {}
    for index, arg in enumerate(args):
        if arg == '-f':
            key, _, value = args[index + 1].partition('=')
            fields.setdefault(key, []).append(value)
    data = load('issues.json', [])
    number = 500 + len(data)
    data.append({
        'number': number, 'state': 'open', 'title': fields['title'][0], 'body': fields['body'][0],
        'labels': fields.get('labels[]', []), 'user': {'login': login, 'id': 42, 'type': 'Bot'},
        'pull_request': None, 'assignees': fields.get('assignees[]', []),
    })
    save('issues.json', data)
elif single:
    number = single.group(1)
    states = load('states.json', {})
    print(json.dumps({'state': states.get(number, 'open'), 'labels': load('labels.json', {}).get(number, [])}))
elif args[:1] == ['api'] or args[:2] == ['label', 'create']:
    pass
else:
    raise RuntimeError(args)
"""

ISSUE = 2
PR_URL = "https://github.com/org/name/pull/34"


def plan_with():
    return {
        "version": 1,
        "artifact_roots": [],
        "issues": [{"number": n, "title": f"T{n}", "body": ""} for n in (1, 2)],
        "jobs": [
            {
                "id": "fix-a",
                "issues": [ISSUE],
                "context_issues": [],
                "objective": "x",
                "likely_paths": ["src"],
                "acceptance": ["a"],
                "depends_on": [],
                "feature": False,
            }
        ],
        "dispositions": [{"issue": ISSUE, "status": "job", "job": "fix-a", "reason": "Concrete fix"}],
    }


def observation(title="The sandbox has no jq binary", detail="Every job reports failing tests that need jq.", **extra):
    return {"kind": "follow_up", "title": title, "detail": detail, **extra}


def cleaned(*raws, by="build"):
    """The stored form of raw worker observations (what ``Runner.observations`` puts in the job state)."""
    with contextlib.redirect_stderr(io.StringIO()):
        return merge([], parse_observations(list(raws), "fix-a", by))


def quietly(function, *args, **kwargs):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        result = function(*args, **kwargs)
    return result, err.getvalue()


class ParseTests(unittest.TestCase):
    def test_a_valid_observation_is_kept_with_its_fingerprint(self):
        items, err = quietly(
            parse_observations,
            [observation(paths=["src/a.py", "docs/b.md"], kind="bug_found")],
            "fix-a",
            "review",
        )
        self.assertEqual(err, "")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["kind"], item["by"]), ("bug_found", "review"))
        self.assertEqual(item["title"], "The sandbox has no jq binary")
        self.assertEqual(item["paths"], ["src/a.py", "docs/b.md"])
        self.assertEqual(item["fp"], fingerprint(item["title"], item["paths"]))
        self.assertEqual(stored(item), item)

    def test_nothing_to_report_is_not_an_error(self):
        for raw in (None, []):
            self.assertEqual(quietly(parse_observations, raw, "fix-a"), ([], ""))

    def test_each_dropped_observation_is_one_fixed_line_and_the_rest_are_kept(self):
        raw = [
            observation(),
            "not an object",
            {**observation(), "kind": "gossip"},
            {**observation(), "title": 5},
            {**observation(), "paths": "src/a.py"},
            {**observation(title="x" * 101)},
            {**observation(detail="y" * 1001)},
            {**observation(paths=["a"] * 6)},
            {**observation(paths=["a" * 201])},
            observation(title="<b></b><i></i><u></u>", detail=SECRET),
            observation(title="A fine title about nothing", detail="<script></script><b></b>"),
            observation(title="A second useful observation", detail="It is a real one."),
        ]
        with patch.object(observed_issues, "MAX_OBSERVATIONS", 50):  # the bound on a result is tested apart
            items, err = quietly(parse_observations, raw, "fix-a")
        self.assertEqual([item["title"] for item in items], ["The sandbox has no jq binary", "A second useful observation"])
        reasons = ["shape", "kind", "shape", "shape", "oversized", "oversized", "oversized", "oversized", "title", "detail"]
        self.assertEqual(err.splitlines(), [f"factory: observation dropped job=fix-a reason={reason}" for reason in reasons])
        self.assertNotIn(SECRET, err)

    def test_a_result_that_is_not_a_list_is_one_dropped_line(self):
        for raw in ("text", {"kind": "environment"}, 7, True):
            self.assertEqual(
                quietly(parse_observations, raw, "fix-a"),
                ([], "factory: observation dropped job=fix-a reason=type\n"),
            )

    def test_items_past_the_schema_bound_are_dropped_one_line_each(self):
        raw = [observation(title=f"Observation number {n} of many") for n in range(schemas.MAX_OBSERVATIONS + 3)]
        items, err = quietly(parse_observations, raw, "fix-a")
        self.assertEqual(len(items), schemas.MAX_OBSERVATIONS)
        self.assertEqual(err.splitlines(), ["factory: observation dropped job=fix-a reason=excess"] * 3)

    def test_it_never_raises_on_garbage(self):
        garbage = [None, 1, 2.5, [], [[]], {}, {"kind": []}, {"kind": "environment", "title": None}, object(), {"paths": [None]}]
        for entry in garbage:
            with self.subTest(entry=repr(entry)):
                items, err = quietly(parse_observations, [entry], "fix-a")
                self.assertEqual(items, [])
                self.assertEqual(err.count("\n"), 1)

    def test_a_job_id_that_is_not_a_name_is_not_printed(self):
        _, err = quietly(parse_observations, ["x"], f"bad job\nid {SECRET}")
        self.assertEqual(err, "factory: observation dropped job=unknown reason=shape\n")

    def test_no_link_image_mention_html_or_secret_survives_in_the_detail_or_the_title(self):
        hostile = {
            "link": "See https://evil.example/x now and keep reading this sentence",
            "markdown link": "Click [here](http://evil.example) for details about the sandbox problem",
            "image": "Look ![pixel](http://evil.example/p.png) for details about the sandbox",
            "mention": "Ping @octocat and @org/team about this sandbox problem please",
            "html": "Details <script>alert(1)</script> about the sandbox problem and more",
            "secret": f"The token={TOKEN} was echoed by the tool during the run",
            "bare secret": f"The tool printed {TOKEN} while it was running in the sandbox",
            "host path": "The file /etc/passwd was readable from inside the sandbox run",
            "reference": "This is the same as #99 and see GH-12 and other/repo#5 for the rest",
            "schemeless": "Visit www.evil.example or mailto:a@b.example for details of this",
            "entity": "Ping &#64;octocat or &amp;#64;octocat about the sandbox problem",
        }
        for name, text in hostile.items():
            for field in ("title", "detail"):
                with self.subTest(name=name, field=field):
                    raw = observation(title=text, detail="A plain and harmless sentence of detail.")
                    if field == "detail":
                        raw = observation(detail=text)
                    (item,) = cleaned(raw)
                    shown = item[field]
                    self.assertNotRegex(shown, r"(?i)https?://|www\.|mailto:")
                    self.assertNotRegex(shown, r"@(?=\w)")
                    self.assertNotRegex(shown, r"#\d")
                    self.assertNotIn("<", shown)
                    self.assertNotIn("](", shown)
                    self.assertNotIn(TOKEN, shown)
                    self.assertNotIn("/etc/passwd", shown)

    def test_the_literal_secret_values_of_the_process_are_redacted(self):
        with patch.dict(os.environ, {"MY_SERVICE_TOKEN": "correct-horse-battery-staple"}):
            item = cleaned(observation(detail="The service answered with correct-horse-battery-staple in a header."))[0]
        self.assertNotIn("correct-horse-battery-staple", item["detail"])

    def test_a_title_is_plain_text_without_backticks(self):
        item = cleaned(observation(title="Use `rm -rf` carefully in scripts"))[0]
        self.assertNotIn("`", item["title"])

    def test_only_plain_repository_relative_paths_survive(self):
        cases = [
            (["src/ok.py", "../etc/passwd", "/abs/path.py", "a b.py", "-rf"], ["src/ok.py"]),
            (["x/../y", "./z.py", "dir//f.py", "src/ok.py", ".github/workflows/ci.yml"], ["src/ok.py", ".github/workflows/ci.yml"]),
            (["a;b", "a@b", "ok\n.py", "~/x", "\u00e9.py"], []),
            (["src/ok.py", "src/ok.py"], ["src/ok.py"]),
        ]
        for number, (paths, kept) in enumerate(cases):
            with self.subTest(paths=paths):
                (item,) = cleaned(observation(title=f"Observation number {number} with paths", paths=paths))
                self.assertEqual(item["paths"], kept)
        self.assertTrue(valid_path("a/b-c_d.e+f"))
        for bad in ("", "/", "..", ".", "a/", "a b", "\u00e9.py", None, 5):
            self.assertFalse(valid_path(bad), bad)

    def test_the_fingerprint_follows_the_normalised_title_and_the_sorted_paths_only(self):
        base = fingerprint("The sandbox has no jq", ["b", "a"])
        self.assertEqual(base, fingerprint("  the SANDBOX   has no jq ", ["a", "b", "a"]))
        self.assertNotEqual(base, fingerprint("The sandbox has no jq", ["a"]))
        self.assertNotEqual(base, fingerprint("The sandbox has no ps", ["a", "b"]))
        first = cleaned(observation())[0]
        again = cleaned({**observation(), "kind": "bug_found", "detail": "Said differently, with other words."})[0]
        self.assertEqual(first["fp"], again["fp"])  # the kind and the detail are not part of the identity

    def test_a_limit_of_the_worker_sandbox_is_never_opened_in_the_target_repository(self):
        """Seen in a target repository (#547, #550 and #551): a missing node, uv or ruff in Factory's sandbox is not the target's defect."""
        env = {**observation(title="Worker sandbox has no Node toolchain"), "kind": "environment"}
        kept = observation(title="A real follow-up for the maintainers")
        items, err = quietly(parse_observations, [env, kept], "fix-a")
        self.assertEqual([item["title"] for item in items], ["A real follow-up for the maintainers"])
        self.assertEqual(err, "factory: observation dropped job=fix-a reason=factory_scope\n")
        # An item an older supervisor already stored in a job's state is not opened either.
        old = {**cleaned(observation())[0], "kind": "environment"}
        self.assertIsNone(stored(old))
        self.assertEqual(merge([], [old]), [])
        # The schema still accepts the kind, so a worker that reports it does not fail its result.
        build = {"summary": "s", "blocked": False, "tests": [], "feature": False, "observations": [env]}
        self.assertEqual(schemacheck.errors(build, schemas.BUILDER), [])

    def test_the_worker_prompts_do_not_offer_the_environment_kind(self):
        prompts = Path(observed_issues.__file__).parent / "prompts"
        for name in ("builder", "reviewer"):
            text = (prompts / f"{name}.md").read_text()
            self.assertNotIn('"environment"', text, name)
            self.assertIn("is not an observation", text, name)

    def test_stored_refuses_a_tampered_item(self):
        item = cleaned(observation(paths=["a.py"]))[0]
        for change in (
            {"fp": "0" * 32},
            {"title": "A different title entirely"},
            {"paths": ["../x"]},
            {"kind": "gossip"},
            {"by": "verify"},
            {"detail": ""},
            {"detail": "z" * 1001},
            {"fp": 5},
        ):
            with self.subTest(change=change):
                self.assertIsNone(stored({**item, **change}))
        self.assertIsNone(stored("text"))
        self.assertIsNone(stored(None))

    def test_merge_keeps_one_item_per_fingerprint_in_order(self):
        one = cleaned(observation(title="First observation to keep"))
        two = cleaned(observation(title="Second observation to keep"))
        merged = merge(one, [*two, *one, "junk", {"fp": "x"}])
        self.assertEqual([item["title"] for item in merged], ["First observation to keep", "Second observation to keep"])
        self.assertEqual(merge("not a list", one), one)

    def test_the_kinds_of_the_schema_and_of_the_template_are_the_same(self):
        self.assertEqual(set(observed_issues.KINDS), set(schemas.OBSERVATION_KINDS))
        self.assertTrue(all(observed_issues.KINDS.values()))


class SchemaTests(unittest.TestCase):
    def test_builders_and_reviewers_may_report_and_verifiers_may_not(self):
        for name in ("BUILDER", "REVIEWER"):
            field = getattr(schemas, name)["properties"]["observations"]
            self.assertEqual(field["maxItems"], schemas.MAX_OBSERVATIONS)
            self.assertNotIn("observations", getattr(schemas, name).get("required", []))
        self.assertNotIn("observations", schemas.VERIFY["properties"])
        self.assertNotIn("observations", schemas.PLANNER["properties"])

    def test_a_result_with_or_without_observations_matches_the_schema(self):
        build = {"summary": "s", "blocked": False, "tests": [], "feature": False}
        self.assertEqual(schemacheck.errors(build, schemas.BUILDER), [])
        with_items = {**build, "observations": [observation(paths=["a.py"])]}
        self.assertEqual(schemacheck.errors(with_items, schemas.BUILDER), [])
        self.assertEqual(schemacheck.errors({"pass": True, "findings": [], "observations": []}, schemas.REVIEWER), [])
        bad = {**build, "observations": [{**observation(), "kind": "gossip"}]}
        self.assertTrue(schemacheck.errors(bad, schemas.BUILDER))
        extra = {**build, "observations": [{**observation(), "link": "https://x.example"}]}
        self.assertTrue(schemacheck.errors(extra, schemas.BUILDER))
        missing = {**build, "observations": [{"kind": "follow_up", "title": "A title that is long enough"}]}
        self.assertTrue(schemacheck.errors(missing, schemas.BUILDER))

    def test_the_schema_bounds_are_the_ones_the_supervisor_applies(self):
        items = schemas.BUILDER["properties"]["observations"]["items"]["properties"]
        self.assertEqual(items["title"]["maxLength"], schemas.OBSERVATION_TITLE_MAX)
        self.assertEqual(items["detail"]["maxLength"], schemas.OBSERVATION_DETAIL_MAX)
        self.assertEqual(items["paths"]["maxItems"], schemas.OBSERVATION_MAX_PATHS)
        self.assertEqual(items["paths"]["items"]["maxLength"], schemas.OBSERVATION_PATH_MAX)

    def test_the_worker_prompts_say_what_to_report_and_what_not_to(self):
        prompts = Path(observed_issues.__file__).parent / "prompts"
        for name in ("builder", "reviewer"):
            text = (prompts / f"{name}.md").read_text()
            self.assertIn('"observations"', text, name)
            self.assertIn("never builds it by itself", text, name)
            self.assertIn("task data", text, name)
        reviewer = (prompts / "reviewer.md").read_text()
        self.assertIn("A defect in THIS diff", reviewer)
        self.assertIn("always a finding, never an observation", reviewer)
        builder = (prompts / "builder.md").read_text()
        self.assertIn("Do not report a defect in your own change", builder)
        self.assertNotIn("observations", (prompts / "verify.md").read_text())


class RenderTests(unittest.TestCase):
    def render(self, **kwargs):
        item = kwargs.pop("item", None) or cleaned(observation(paths=["src/a.py"]))[0]
        arguments = {"issues": [ISSUE], "pr": 34, "job": "fix-a", "run": "auto-1", **kwargs}
        return render(item, **arguments)

    def test_only_positive_whole_numbers_are_linked(self):
        _, body = self.render(issues=[True, 0, -3, "7", 2.0, 5], pr=True)
        self.assertIn("(#5)", body)
        self.assertNotIn("Pull request", body)
        _, body = self.render(issues=[], pr=0)
        self.assertNotIn("Pull request", body)
        self.assertNotIn("(#", body)

    def test_the_arguments_are_not_changed(self):
        item = cleaned(observation())[0]
        before = dict(item)
        issues = [3, 1]
        self.render(item=item, issues=issues, job="NOT A JOB", run="not a run!")
        self.assertEqual((item, issues), (before, [3, 1]))
        title, body = self.render(item=item, job="NOT A JOB", run="not a run!")
        self.assertIn("of job `unknown` in run `unknown`", body)

    def test_the_template_is_fixed_and_the_detail_is_in_one_code_block(self):
        item = cleaned(observation(paths=["src/a.py", "src/b.py"]))[0]
        title, body = self.render(item=item)
        self.assertEqual(title, "[Factory observed] The sandbox has no jq binary")
        lines = body.splitlines()
        self.assertEqual(lines[0], f"<!-- factory-observed:v1 fp={item['fp']} -->")
        self.assertEqual(lines[1], "**Factory observed: a follow-up the work implies**")
        self.assertEqual(lines.count("```text"), 1)
        self.assertEqual(lines.count("```"), 1)
        start, end = lines.index("```text"), lines.index("```")
        inside = "\n".join(lines[start + 1 : end])
        self.assertIn("Every job reports failing tests that need jq.", inside)
        self.assertIn("Paths: src/a.py, src/b.py", inside)
        outside = "\n".join([*lines[1:start], *lines[end + 1 :]])
        self.assertNotIn("jq", outside)
        self.assertNotIn("src/a.py", outside)
        self.assertIn("while it worked on the issue (#2). Pull request: #34.", body)
        self.assertIn("Reported by the builder of job `fix-a` in run `auto-1` · Kind: `follow_up`", body)
        self.assertIn("This issue was opened automatically by Factory", body)

    def test_nothing_in_the_page_can_link_mention_or_render(self):
        item = cleaned(observation(detail="@octocat https://evil.example ![x](http://e.example) <img src=x> `tick`"))[0]
        title, body = self.render(item=item)
        self.assertNotIn("@", f"{title}{body}")
        self.assertNotRegex(f"{title}{body}", r"(?i)https?://")
        self.assertEqual(body.count("<"), 1)  # the marker, and nothing else
        self.assertNotIn("`tick`", body)

    def test_a_long_detail_is_wrapped_inside_the_block(self):
        item = cleaned(observation(detail=" ".join(["word"] * 190)))[0]
        _, body = self.render(item=item)
        lines = body.splitlines()
        inside = lines[lines.index("```text") + 1 : lines.index("```")]
        self.assertGreater(len(inside), 5)
        self.assertTrue(all(len(line) <= observed_issues.DETAIL_WIDTH for line in inside))

    def test_the_source_the_pull_request_and_the_names_are_only_what_factory_produced(self):
        _, body = self.render(issues=[7, 3, 3, -1, "9", True], pr=None, job="bad job\n@x", run="../run")
        self.assertIn("worked on the issue (#3, #7).", body)
        self.assertNotIn("Pull request", body)
        self.assertIn("job `unknown` in run `unknown`", body)
        _, body = self.render(pr="https://evil.example")
        self.assertNotIn("evil", body)

    def test_the_trigger_label_is_named_but_never_applied(self):
        _, body = self.render(trigger_label="ship it")
        self.assertIn("apply the `ship it` label yourself", body)
        _, body = self.render(trigger_label="`; rm -rf /")
        self.assertIn("apply the `factory-build` label yourself", body)
        self.assertIn("Factory never applies that label", body)

    def test_a_reviewer_and_every_kind_have_their_own_wording(self):
        seen = set()
        for kind in sorted(set(observed_issues.KINDS) - observed_issues.NOT_OPENED):
            item = cleaned({**observation(), "kind": kind}, by="review")[0]
            _, body = self.render(item=item)
            seen.add(body.splitlines()[1])
            self.assertIn("Reported by the reviewer of job", body)
        self.assertEqual(len(seen), len(observed_issues.KINDS) - len(observed_issues.NOT_OPENED))

    def test_an_item_that_is_not_an_observation_is_refused(self):
        with self.assertRaises(ValueError):
            render({"kind": "environment"}, [1], None, "fix-a", "run-1")


class FakeGhCase(unittest.TestCase):
    def setUp(self):
        issue_labels.reset_report()
        observed_issues.reset_report()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n{FAKE_GH}")
        gh.chmod(0o755)
        self.work = self.root / "work"
        self.work.mkdir()
        env = patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.root), "FAKE_SECRET": SECRET},
        )
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "FAKE_OBS_FAIL",
            "FAKE_OBS_POST_FAIL",
            "FAKE_ISSUE_READ_FAIL",
            "FAKE_LOGIN",
            "FAKE_WHOAMI",
            observed_issues.ENV_SWITCH,
            issue_comments.ENV_SWITCH,
            issue_comments.ENV_CAP,
        ):
            os.environ.pop(name, None)

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def posts(self):
        """The calls that create an issue."""
        return [c for c in self.calls() if c[:3] == ["api", "-X", "POST"] and c[3].endswith("/issues")]

    def lists(self):
        return [c for c in self.calls() if "--paginate" in c]

    def issues(self):
        store = self.root / "issues.json"
        return json.loads(store.read_text()) if store.exists() else []

    def add_issue(self, **fields):
        data = self.issues()
        data.append(
            {
                "number": 900 + len(data),
                "state": "open",
                "title": "t",
                "body": "",
                "labels": ["factory:observed"],
                "user": {"login": "factory-bot[bot]", "id": 42, "type": "Bot"},
                "pull_request": None,
                **fields,
            }
        )
        (self.root / "issues.json").write_text(json.dumps(data))

    def set_labels(self, number, *names):
        store = self.root / "labels.json"
        data = json.loads(store.read_text()) if store.exists() else {}
        data[str(number)] = list(names)
        store.write_text(json.dumps(data))

    def writer(self, **kwargs):
        arguments = {"enabled": True, "fence": None, **kwargs}
        return ObservedIssues(self.work, "org/name", "run-1", **arguments)

    def flush(self, writer, *raws, issues=(ISSUE,), pr=None, job="fix-a", by="build"):
        return quietly(writer.flush, cleaned(*raws, by=by), list(issues), pr, job)


class OpenTests(FakeGhCase):
    def test_a_valid_observation_opens_one_issue_with_the_fixed_template_and_label(self):
        writer = self.writer()
        opened, err = self.flush(writer, observation(paths=["src/a.py"]), pr=34)
        self.assertEqual((opened, err), (1, ""))
        (call,) = self.posts()
        self.assertEqual(call[3], "repos/org/name/issues")
        fields = [call[i + 1] for i, arg in enumerate(call) if arg == "-f"]
        self.assertEqual([f.split("=", 1)[0] for f in fields], ["title", "body", "labels[]"])
        self.assertEqual(fields[2], "labels[]=factory:observed")
        (issue,) = self.issues()
        self.assertEqual(issue["labels"], ["factory:observed"])
        self.assertEqual(issue["assignees"], [])
        self.assertEqual(issue["title"], "[Factory observed] The sandbox has no jq binary")
        self.assertTrue(issue["body"].startswith("<!-- factory-observed:v1 fp="))
        self.assertIn("#2", issue["body"])
        self.assertIn("Pull request: #34.", issue["body"])
        self.assertIn("run `run-1`", issue["body"])
        (label,) = [c for c in self.calls() if c[:2] == ["label", "create"]]
        self.assertEqual(label[:7], ["label", "create", "factory:observed", "--force", "--color", "fef2c0", "--description"])
        self.assertEqual(label[8:], ["-R", "org/name"])

    def test_it_never_applies_the_trigger_label_or_an_assignee(self):
        writer = self.writer(trigger_label="ship it")
        self.flush(writer, observation(), observation(title="Another observation of note"))
        for issue in self.issues():
            self.assertEqual(issue["labels"], ["factory:observed"])
            self.assertEqual(issue["assignees"], [])
        everything = json.dumps(self.calls())
        self.assertNotIn("assignees", everything)
        self.assertNotIn("labels[]=factory-build", everything)
        self.assertNotIn("labels[]=ship it", everything)

    def test_the_same_observation_again_opens_nothing_even_after_a_restart(self):
        self.flush(self.writer(), observation())
        again = self.writer()  # a new process: it has to ask GitHub
        self.assertEqual(self.flush(again, observation())[0], 0)
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(len(self.lists()), 2)
        self.assertEqual(len(self.posts()), 1)

    def test_the_same_process_does_not_even_ask_again(self):
        writer = self.writer()
        self.flush(writer, observation())
        before = len(self.calls())
        self.assertEqual(self.flush(writer, observation())[0], 0)
        self.assertEqual(len(self.calls()), before)

    def test_a_changed_fingerprint_opens_another_issue(self):
        writer = self.writer()
        self.flush(writer, observation())
        self.assertEqual(self.flush(writer, observation(title="The sandbox has no ps binary either"))[0], 1)
        self.assertEqual(self.flush(writer, observation(paths=["src/a.py"]))[0], 1)
        self.assertEqual(len(self.issues()), 3)

    def test_the_same_observation_twice_in_one_result_opens_one_issue(self):
        self.assertEqual(self.flush(self.writer(), observation(), observation(detail="Said again, differently."))[0], 1)
        self.assertEqual(len(self.issues()), 1)

    def test_a_closed_issue_still_counts_so_a_wont_fix_is_not_reopened(self):
        item = cleaned(observation())[0]
        self.add_issue(state="closed", body=f"{observed_issues.marker(item['fp'])}\nbody")
        self.assertEqual(self.flush(self.writer(), observation())[0], 0)
        self.assertEqual(self.posts(), [])
        query = self.lists()[0][2]
        self.assertIn("state=all", query)
        self.assertIn("labels=factory%3Aobserved", query)

    def test_a_marker_forged_by_somebody_else_hides_nothing(self):
        item = cleaned(observation())[0]
        marked = f"{observed_issues.marker(item['fp'])}\nforged"
        self.add_issue(body=marked, user={"login": "mallory", "id": 7, "type": "User"})
        self.add_issue(body=marked, user={"login": "other-app[bot]", "id": 8, "type": "Bot"})
        self.add_issue(body=marked, pull_request={"url": "x"})
        self.assertEqual(self.flush(self.writer(), observation())[0], 1)
        self.assertEqual(len(self.posts()), 1)

    def test_the_app_identity_is_the_bot_user_id_when_there_is_one(self):
        item = cleaned(observation())[0]
        self.add_issue(body=observed_issues.marker(item["fp"]), user={"login": "app[bot]", "id": 42, "type": "Bot"})
        with patch.object(ObservedIssues, "_identity", staticmethod(lambda: (42, False))):
            self.assertEqual(self.flush(self.writer(), observation())[0], 0)
            self.assertIn(".user.id == 42", self.lists()[0][4])
        self.assertEqual(self.posts(), [])
        with patch.object(ObservedIssues, "_identity", staticmethod(lambda: (43, False))):
            self.assertEqual(self.flush(self.writer(), observation())[0], 1)

    def test_an_unknown_identity_opens_nothing_and_says_so_once(self):
        os.environ["FAKE_WHOAMI"] = "not a login!"
        writer = self.writer()
        opened, err = self.flush(writer, observation())
        self.assertEqual((opened, err), (0, "factory: observed issue failed (LookupError)\n"))
        self.assertEqual(self.posts(), [])

    def test_every_call_targets_the_configured_repository_only(self):
        self.flush(self.writer(), observation(detail="See other/repo#5 and https://github.com/other/repo/issues/5 here"))
        for call in self.calls():
            for arg in call:
                if arg.startswith("repos/"):
                    self.assertTrue(arg.startswith("repos/org/name/"), arg)
        self.assertIn("-R", [a for c in self.calls() if c[:2] == ["label", "create"] for a in c])
        self.assertNotIn("other/repo#5", self.issues()[0]["body"])
        self.assertNotIn("github.com", self.issues()[0]["body"])

    def test_there_is_no_cap_on_how_many_issues_are_opened(self):
        writer = self.writer()
        total = 0
        for round_ in range(4):  # four results of five observations each, plus a few more
            raws = [observation(title=f"Observation {round_}-{n} that deserves an issue") for n in range(schemas.MAX_OBSERVATIONS)]
            total += self.flush(writer, *raws, job=f"job-{round_}")[0]
        self.assertEqual(total, 20)
        self.assertEqual(len(self.issues()), 20)
        # Neither the commenter's cap nor its kill switch is this feature's.
        os.environ[issue_comments.ENV_CAP] = "1"
        os.environ[issue_comments.ENV_SWITCH] = "off"
        other = self.writer()
        self.assertTrue(other.enabled)
        self.assertEqual(self.flush(other, observation(title="Yet another observation to open"))[0], 1)
        self.assertEqual(len(self.issues()), 21)
        self.assertFalse(hasattr(observed_issues, "DEFAULT_CAP"))

    def test_the_label_is_created_once_per_process(self):
        writer = self.writer()
        self.flush(writer, observation(), observation(title="Another observation of note"))
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["label", "create"]]), 1)


class SwitchTests(FakeGhCase):
    def test_the_repository_setting_suppresses_everything(self):
        writer = self.writer(enabled=False)
        self.assertFalse(writer.active)
        self.assertEqual(self.flush(writer, observation())[0], 0)
        self.assertFalse(writer.due(None, {"status": "failed", "observed": cleaned(observation())}))
        self.assertEqual(self.calls(), [])

    def test_the_global_kill_switch_suppresses_everything(self):
        for value in ("off", "OFF", "0", "false", "no", "disabled", " off "):
            with self.subTest(value=value):
                os.environ[observed_issues.ENV_SWITCH] = value
                writer = self.writer()
                self.assertFalse(writer.enabled)
                self.assertEqual(self.flush(writer, observation())[0], 0)
        self.assertEqual(self.calls(), [])
        os.environ[observed_issues.ENV_SWITCH] = "on"
        self.assertTrue(self.writer().enabled)

    def test_for_run_follows_the_repository_config(self):
        directory = self.root / "auto-20260101T000000Z"
        on = observed_issues.for_run(self.work, RepoConfig(repo="org/name"), directory)
        self.assertTrue(on.enabled)
        self.assertEqual((on.slug, on.run), ("org/name", "auto-20260101T000000Z"))
        self.assertFalse(observed_issues.for_run(self.work, RepoConfig(repo="org/name", observed_issues=False), directory).enabled)
        # No configured repository: the target would be guessed from a checkout, so nothing is written.
        self.assertFalse(observed_issues.for_run(self.work, RepoConfig(repo=""), directory).enabled)
        self.assertFalse(observed_issues.for_run(self.work, object(), directory).enabled)
        label = observed_issues.for_run(self.work, RepoConfig(repo="org/name", auto_build_label="ship it"), directory)
        self.assertEqual(label.trigger_label, "ship it")
        os.environ[observed_issues.ENV_SWITCH] = "off"
        self.assertFalse(observed_issues.for_run(self.work, RepoConfig(repo="org/name"), directory).enabled)


class FailureTests(FakeGhCase):
    def assert_quiet_failure(self, mode, expected):
        os.environ["FAKE_OBS_FAIL"] = mode
        writer = self.writer()
        opened, err = self.flush(writer, observation())
        self.assertEqual((opened, err), (0, f"factory: observed issue failed ({expected})\n"))
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.posts(), [])  # fail closed: when it cannot look, it does not write
        self.assertEqual(self.flush(writer, observation(title="Another observation of note"))[1], "")  # one line per process

    def test_403(self):
        self.assert_quiet_failure("403", "CalledProcessError")

    def test_malformed_answer(self):
        self.assert_quiet_failure("garbage", "JSONDecodeError")

    def test_a_timeout_is_bounded_and_reported(self):
        os.environ["FAKE_OBS_FAIL"] = "timeout"
        started = time.monotonic()
        with patch.object(observed_issues, "LIST_TIMEOUT", 0.3):
            opened, err = self.flush(self.writer(), observation())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((opened, err), (0, "factory: observed issue failed (TimeoutExpired)\n"))

    def test_a_refused_creation_is_retried_by_the_next_flush(self):
        os.environ["FAKE_OBS_POST_FAIL"] = "1"
        writer = self.writer()
        opened, err = self.flush(writer, observation())
        self.assertEqual((opened, err), (0, "factory: observed issue failed (CalledProcessError)\n"))
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.issues(), [])
        del os.environ["FAKE_OBS_POST_FAIL"]
        self.assertEqual(self.flush(writer, observation())[0], 1)
        self.assertEqual(len(self.issues()), 1)

    def test_it_rests_after_three_failures_in_a_row_and_probes_afterwards(self):
        os.environ["FAKE_OBS_FAIL"] = "403"
        now = [100.0]
        writer = self.writer(clock=lambda: now[0])
        for number in range(5):
            self.flush(writer, observation(title=f"Observation {number} that cannot be listed"))
        self.assertEqual(len(self.lists()), observed_issues.MAX_CONSECUTIVE_FAILURES)
        self.assertFalse(writer.active)
        now[0] += issue_comments.COOLDOWN_SECONDS + 1
        del os.environ["FAKE_OBS_FAIL"]
        self.assertEqual(self.flush(writer, observation(title="Observation that is finally written"))[0], 1)
        self.assertTrue(writer.active)

    def test_a_probe_that_fails_rests_again_at_once(self):
        os.environ["FAKE_OBS_FAIL"] = "403"
        now = [100.0]
        writer = self.writer(clock=lambda: now[0])
        for number in range(3):
            self.flush(writer, observation(title=f"Observation {number} that cannot be listed"))
        now[0] += issue_comments.COOLDOWN_SECONDS + 1
        self.flush(writer, observation(title="The probe that fails again now"))
        self.assertFalse(writer.active)
        self.assertEqual(len(self.lists()), observed_issues.MAX_CONSECUTIVE_FAILURES + 1)

    def test_a_lost_lock_stops_it_quietly(self):
        def fence():
            raise LockLost("gone")

        writer = self.writer(fence=fence)
        self.assertEqual(self.flush(writer, observation()), (0, ""))
        self.assertTrue(writer.lost)
        self.assertEqual(self.calls(), [])
        self.assertFalse(writer.active)

    def test_an_unexpected_exception_is_swallowed(self):
        writer = self.writer()
        with patch.object(writer, "_flush", side_effect=RuntimeError(f"boom {SECRET}")):
            opened, err = self.flush(writer, observation())
        self.assertEqual((opened, err), (0, "factory: observed issue failed (RuntimeError)\n"))

    def test_garbage_in_the_state_is_ignored(self):
        writer = self.writer()
        self.assertEqual(quietly(writer.flush, "nope", [ISSUE], None, "fix-a"), (0, ""))
        self.assertEqual(quietly(writer.flush, [{"kind": "x"}, 5, None], [ISSUE], None, "fix-a"), (0, ""))
        self.assertEqual(self.calls(), [])


class JobHookTests(FakeGhCase):
    def labeller(self, plan=None, **kwargs):
        writer = kwargs.pop("writer", None) or self.writer()
        return IssueLabels(self.work, "org/name", plan or plan_with(), observed=writer, **kwargs), writer

    def terminal(self, status="failed", **extra):
        return {"status": status, "observed": cleaned(observation()), **extra}

    def test_a_job_that_ends_blocked_opens_the_issue_without_a_pull_request_link(self):
        labels, _ = self.labeller()
        _, err = quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal("blocked", error="needs a tool"))
        self.assertEqual(err, "")
        (issue,) = self.issues()
        self.assertIn("#2", issue["body"])
        self.assertNotIn("Pull request", issue["body"])

    def test_a_published_job_links_its_pull_request(self):
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal("pr_ready", pr=PR_URL))
        (issue,) = self.issues()
        self.assertIn("Pull request: #34.", issue["body"])

    def test_every_terminal_status_opens_and_no_other_does(self):
        for status in sorted(observed_issues.STATUSES):
            with self.subTest(status=status):
                (self.root / "issues.json").unlink(missing_ok=True)
                labels, _ = self.labeller(writer=self.writer())
                quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal(status))
                self.assertEqual(len(self.issues()), 1)
        for status in ("queued", "running", "waiting_review", "interrupted"):
            with self.subTest(status=status):
                (self.root / "issues.json").unlink(missing_ok=True)
                labels, _ = self.labeller(writer=self.writer())
                quietly(labels.on_job, "fix-a", {"status": "queued"}, self.terminal(status))
                self.assertEqual(self.issues(), [])

    def test_a_state_change_that_is_not_about_the_end_opens_nothing_twice(self):
        labels, _ = self.labeller()
        done = self.terminal("failed")
        quietly(labels.on_job, "fix-a", {"status": "running"}, done)
        before = len(self.calls())
        quietly(labels.on_job, "fix-a", done, {**done, "usage": [1]})  # same status, pull request and observations
        self.assertEqual(len(self.calls()), before)

    def test_a_job_without_observations_costs_no_call(self):
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, {"status": "failed", "error": "x"})
        self.assertEqual(self.lists(), [])
        self.assertEqual(self.posts(), [])

    def test_a_key_the_plan_does_not_own_is_ignored(self):
        labels, _ = self.labeller()
        quietly(labels.on_job, "other-job", {"status": "running"}, self.terminal("failed"))
        self.assertEqual(self.calls(), [])

    def test_the_real_state_hook_never_sees_a_failure_and_is_not_held_up(self):
        os.environ["FAKE_OBS_FAIL"] = "timeout"
        plan = plan_with()
        directory = self.root / "run"
        directory.mkdir()
        state = State(directory, plan)
        labels, _ = self.labeller(plan)
        state.observer = labels.on_job
        started = time.monotonic()
        with patch.object(observed_issues, "LIST_TIMEOUT", 0.3):
            _, err = quietly(state.update, "fix-a", status="failed", error="no way forward", observed=cleaned(observation()))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(state.data["jobs"]["fix-a"]["status"], "failed")
        self.assertEqual(err, "factory: observed issue failed (TimeoutExpired)\n")
        self.assertEqual(json.loads((directory / "state.json").read_text())["jobs"]["fix-a"]["status"], "failed")

    def test_an_unexpected_exception_in_the_hook_is_swallowed(self):
        labels, writer = self.labeller()
        with patch.object(writer, "due", side_effect=RuntimeError(SECRET)):
            _, err = quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal("failed"))
        self.assertEqual(err, "factory: observed issue failed (RuntimeError)\n")

    def test_reconcile_makes_up_for_a_flush_that_was_missed(self):
        os.environ["FAKE_OBS_FAIL"] = "403"
        labels, _ = self.labeller()
        states = {"fix-a": self.terminal("failed")}
        quietly(labels.on_job, "fix-a", {"status": "running"}, states["fix-a"])
        self.assertEqual(self.issues(), [])
        del os.environ["FAKE_OBS_FAIL"]
        writer = self.writer()  # the next process
        labels = IssueLabels(self.work, "org/name", plan_with(), observed=writer)
        quietly(labels.reconcile, states)
        self.assertEqual(len(self.issues()), 1)
        quietly(labels.reconcile, states)  # and once more: nothing new
        self.assertEqual(len(self.issues()), 1)
        again = IssueLabels(self.work, "org/name", plan_with(), observed=self.writer())
        quietly(again.reconcile, states)
        self.assertEqual(len(self.issues()), 1)

    def test_observe_jobs_alone_does_the_same_for_the_idle_reconcile(self):
        labels, _ = self.labeller()
        states = {"fix-a": self.terminal("pr_incomplete", pr=PR_URL), "gone": {"status": "failed"}}
        quietly(labels.observe_jobs, states)
        self.assertEqual(len(self.issues()), 1)
        before = len(self.calls())
        quietly(labels.observe_jobs, states)  # everything is known: not a single call, not even a read of the source issue
        self.assertEqual(len(self.calls()), before)

    def test_an_issue_in_the_skip_set_still_gets_the_observation_issue(self):
        # The skip set narrows the label and the comment (the other run owns them now), never the observation.
        labels, _ = self.labeller()
        quietly(labels.reconcile, {"fix-a": self.terminal("failed")}, skip={ISSUE})
        self.assertEqual(len(self.issues()), 1)

    def test_never_about_an_issue_a_person_opted_out(self):
        self.set_labels(ISSUE, "factory-ignore")
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal("failed"))
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.lists(), [])

    def test_an_issue_that_cannot_be_read_counts_as_opted_out_until_it_can(self):
        os.environ["FAKE_ISSUE_READ_FAIL"] = "1"
        labels, _ = self.labeller()
        states = {"fix-a": self.terminal("failed")}
        quietly(labels.reconcile, states)
        self.assertEqual(self.issues(), [])
        del os.environ["FAKE_ISSUE_READ_FAIL"]
        labels.read_suspended_until = None
        labels.read_failures = 0
        quietly(labels.reconcile, states)
        self.assertEqual(len(self.issues()), 1)

    def test_the_new_issue_is_never_a_source_for_the_labeller_or_the_commenter(self):
        commenter = issue_comments.IssueComments(self.work, "org/name", "run-1")
        labels, _ = self.labeller(commenter=commenter)
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.terminal("failed", error="no way forward"))
        (issue,) = self.issues()
        mine = f"/issues/{issue['number']}"
        touching = [c for c in self.calls() if any(isinstance(a, str) and mine in a and a.startswith("repos/") for a in c)]
        self.assertEqual(touching, [])
        # Nothing is ever written to an issue Factory opened except by creating it.
        self.assertEqual(len(self.posts()), 1)

    def test_a_person_who_applies_the_trigger_label_gets_the_planned_label_and_keeps_the_origin_label(self):
        # The issue Factory opened is now owned by a job (it was planned after the label was applied).
        labels, _ = self.labeller(writer=self.writer(enabled=False))
        self.set_labels(ISSUE, "factory:observed", "factory-build")
        labels.sync({ISSUE: issue_labels.LABEL_PLANNED})
        deletes = [c for c in self.calls() if c[:3] == ["api", "-X", "DELETE"]]
        self.assertEqual(deletes, [])
        self.assertNotIn("factory:observed", issue_labels.STATUS_LABELS)


class SettleTests(FakeGhCase):
    """What has its issue (or was dismissed on purpose) is written down next to the job states, so that a
    refused flush is asked for again and a settled one is not (``observed.json``; the manager reads it)."""

    def setUp(self):
        super().setUp()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()

    def writer(self, **kwargs):
        return super().writer(**{"directory": self.run_dir, **kwargs})

    def labeller(self, writer=None):
        writer = writer or self.writer()
        return IssueLabels(self.work, "org/name", plan_with(), observed=writer), writer

    def job(self, status="failed"):
        return {"status": status, "observed": cleaned(observation())}

    def pending(self, status="failed"):
        return observed_issues.pending(self.run_dir, {"fix-a": self.job(status)})

    def test_a_flush_that_opened_the_issue_settles_it(self):
        labels, _ = self.labeller()
        self.assertTrue(self.pending())
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        self.assertEqual(len(self.issues()), 1)
        self.assertFalse(self.pending())
        self.assertEqual(observed_issues.read_done(self.run_dir), {"fix-a": {self.job()["observed"][0]["fp"]}})

    def test_a_flush_that_github_refused_stays_pending_until_one_goes_through(self):
        os.environ["FAKE_OBS_FAIL"] = "403"
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        self.assertEqual(self.issues(), [])
        self.assertTrue(self.pending())  # no pull request: only this record brings the run back to the reconcile
        self.assertFalse((self.run_dir / observed_issues.DONE_FILE).exists())
        del os.environ["FAKE_OBS_FAIL"]
        again, _ = self.labeller()  # the next process
        quietly(again.observe_jobs, {"fix-a": self.job()})
        self.assertEqual(len(self.issues()), 1)
        self.assertFalse(self.pending())

    def test_a_settled_observation_costs_the_next_process_no_call_at_all(self):
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        before = len(self.calls())
        again, _ = self.labeller()
        quietly(again.observe_jobs, {"fix-a": self.job()})
        self.assertEqual(len(self.calls()), before)  # neither the list of issues nor a read of the source issue
        self.assertEqual(len(self.issues()), 1)

    def test_what_was_opened_is_recorded_even_when_the_next_one_fails(self):
        labels, writer = self.labeller()
        first, second = cleaned(observation(), observation(title="The sandbox has no curl binary either"))
        original = writer._open
        calls = []

        def flaky(item, *rest):
            calls.append(item["fp"])
            if len(calls) == 2:
                raise RuntimeError("boom")
            return original(item, *rest)

        with patch.object(writer, "_open", side_effect=flaky):
            quietly(labels.on_job, "fix-a", {"status": "running"}, {"status": "failed", "observed": [first, second]})
        self.assertEqual(observed_issues.read_done(self.run_dir), {"fix-a": {first["fp"]}})
        self.assertTrue(observed_issues.pending(self.run_dir, {"fix-a": {"status": "failed", "observed": [first, second]}}))

    def test_an_opted_out_issue_is_settled_for_good_and_costs_no_further_read(self):
        self.set_labels(ISSUE, "factory-ignore")
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        self.assertEqual(self.issues(), [])
        self.assertFalse(self.pending())  # nothing will ever be opened: the idle reconcile must not keep asking
        self.set_labels(ISSUE)
        before = len(self.calls())
        again, _ = self.labeller()
        quietly(again.observe_jobs, {"fix-a": self.job()})
        self.assertEqual((self.issues(), len(self.calls())), ([], before))

    def test_an_issue_that_cannot_be_read_stays_pending_for_the_next_reconcile(self):
        os.environ["FAKE_ISSUE_READ_FAIL"] = "1"
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        self.assertEqual(self.issues(), [])
        self.assertTrue(self.pending())

    def test_an_issue_another_runs_plan_lists_does_not_swallow_the_observation(self):
        # Two runs of one issue (a re-applied trigger after a blocked job) would each leave it to the other.
        labels, _ = self.labeller()
        quietly(labels.reconcile, {"fix-a": self.job()}, skip={ISSUE})
        self.assertEqual(len(self.issues()), 1)
        self.assertIn(f"#{ISSUE}", self.issues()[0]["body"])  # the job worked on it: the issue links it
        self.assertFalse(self.pending())

    def test_a_job_that_owns_no_issue_is_settled_without_a_call(self):
        labels, _ = self.labeller()
        labels.owned["fix-a"] = []
        quietly(labels.observe_jobs, {"fix-a": self.job()})
        self.assertEqual((self.issues(), self.calls()), ([], []))
        self.assertFalse(self.pending())

    def test_only_a_job_that_has_ended_with_a_usable_observation_is_pending(self):
        for status in ("queued", "running", "interrupted", "verifying"):
            self.assertFalse(self.pending(status), status)
        for status in sorted(observed_issues.STATUSES):
            self.assertTrue(self.pending(status), status)
        for jobs in (None, [], {"fix-a": None}, {"fix-a": {"status": "failed"}}, {"fix-a": {"status": "failed", "observed": "x"}},
                     {"fix-a": {"status": "failed", "observed": [{"kind": "bug_found"}, 7, None]}}):
            self.assertFalse(observed_issues.pending(self.run_dir, jobs), jobs)

    def test_the_record_is_read_defensively(self):
        path = self.run_dir / observed_issues.DONE_FILE
        fp = "a" * observed_issues.FINGERPRINT_LENGTH
        for text, expected in (
            ("", {}),
            ("[not json", {}),
            ("[]", {}),
            ('{"done": []}', {}),
            ('{"done": {"fix-a": "x", "fix-b": [1, "zz", null]}}', {"fix-b": set()}),
            (json.dumps({"done": {"fix-a": [fp, "NOT-A-FINGERPRINT"]}}), {"fix-a": {fp}}),
        ):
            path.write_text(text)
            self.assertEqual(observed_issues.read_done(self.run_dir), expected, text)
        path.unlink()
        self.assertEqual(observed_issues.read_done(self.run_dir), {})
        self.assertEqual(observed_issues.read_done(self.root / "missing"), {})

    def test_a_record_that_cannot_be_written_never_costs_the_issue(self):
        writer = self.writer()
        with patch.object(observed_issues, "atomic_json", side_effect=PermissionError(SECRET)):
            opened, err = self.flush(writer, observation())
        self.assertEqual(opened, 1)
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(err, "factory: observed issue failed (PermissionError)\n")
        self.assertTrue(self.pending())  # nothing was written: the next reconcile reads the issues again, opens nothing

    def test_without_a_run_directory_nothing_is_recorded(self):
        writer = ObservedIssues(self.work, "org/name", "run-1", enabled=True)
        self.assertEqual(self.flush(writer, observation())[0], 1)
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_the_record_holds_no_text_of_the_observation(self):
        labels, _ = self.labeller()
        quietly(labels.on_job, "fix-a", {"status": "running"}, self.job())
        text = (self.run_dir / observed_issues.DONE_FILE).read_text()
        self.assertEqual(set(json.loads(text)), {"version", "done"})
        self.assertNotIn("jq", text)


class CliReconcileTests(FakeGhCase):
    """The CLI ``reconcile`` (what the manager runs for an idle run) opens what a finished job observed, with no
    pull request to bring the run back: the real command, a plan on disk and the fake ``gh``."""

    def run_dir(self, name, status=None):
        directory = self.root / "runs" / name
        directory.mkdir(parents=True)
        plan = plan_with()
        plan["issues"] = [item for item in plan["issues"] if item["number"] == ISSUE]  # every issue needs a disposition
        (directory / "plan.json").write_text(json.dumps(plan))
        if status:
            job = {"status": status, "attempt": 1, "observed": cleaned(observation())}
            (directory / "state.json").write_text(json.dumps({"version": 1, "jobs": {"fix-a": job}}))
        return directory

    def reconcile(self, directory, cfg=None):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            cli.reconcile_command(cfg or RepoConfig(repo="org/name"), self.root / "no-clone", directory, None)
        self.assertNotIn(SECRET, err.getvalue() + out.getvalue())
        return err.getvalue()

    def test_a_blocked_job_without_a_pull_request_gets_its_issue_and_a_second_reconcile_opens_nothing(self):
        directory = self.run_dir("run-1", "blocked")
        self.assertEqual(self.reconcile(directory), "")
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(self.issues()[0]["labels"], [observed_issues.LABEL_OBSERVED])
        self.assertTrue(observed_issues.read_done(directory))  # recorded: the manager no longer asks for this run
        self.reconcile(directory)
        self.assertEqual((len(self.issues()), len(self.posts())), (1, 1))

    def test_a_flush_that_github_refused_is_made_up_for_by_the_next_reconcile(self):
        directory = self.run_dir("run-1", "failed")
        os.environ["FAKE_OBS_FAIL"] = "403"
        self.assertEqual(self.reconcile(directory), "factory: observed issue failed (CalledProcessError)\n")
        self.assertEqual(self.issues(), [])
        self.assertEqual(observed_issues.read_done(directory), {})  # nothing is settled: it stays pending
        del os.environ["FAKE_OBS_FAIL"]
        issue_labels.reset_report()
        observed_issues.reset_report()
        self.reconcile(directory)
        self.assertEqual(len(self.issues()), 1)

    def test_a_run_that_another_run_also_lists_still_gets_its_issue(self):
        first = self.run_dir("run-1", "blocked")
        second = self.run_dir("run-2")  # a later re-plan of the same issue: its plan lists the issue too
        self.reconcile(first)
        self.assertEqual(len(self.issues()), 1)
        self.assertEqual(observed_issues.read_done(second), {})
        self.reconcile(first)
        self.assertEqual(len(self.issues()), 1)

    def test_an_older_run_that_lists_the_issue_does_not_swallow_the_observation_either(self):
        self.run_dir("run-0", "pr_ready")
        newer = self.run_dir("run-1", "failed")
        self.reconcile(newer)
        self.assertEqual(len(self.issues()), 1)

    def test_a_repository_that_switched_the_issues_off_opens_nothing(self):
        directory = self.run_dir("run-1", "blocked")
        self.reconcile(directory, RepoConfig(repo="org/name", observed_issues=False))
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.posts(), [])
        self.assertTrue(observed_issues.pending(directory, json.loads((directory / "state.json").read_text())["jobs"]))


class PlannerTests(unittest.TestCase):
    def test_an_observed_issue_nobody_adopted_is_left_out_of_an_unfiltered_listing(self):
        listing = [
            {"number": 1, "labels": [{"name": "bug"}]},
            {"number": 2, "labels": [{"name": "factory:observed"}]},
            {"number": 3, "labels": [{"name": "Factory:Observed "}, {"name": "factory-build"}]},
            {"number": 4, "labels": [{"name": "factory:observed"}, {"name": "ship it"}]},
            {"number": 5, "labels": []},
            {"number": 6},
        ]
        self.assertEqual([i["number"] for i in drop_unadopted(listing, "factory-build")], [1, 3, 5, 6])
        self.assertEqual([i["number"] for i in drop_unadopted(listing, "ship it")], [1, 4, 5, 6])
        self.assertTrue(is_unadopted(listing[1], "factory-build"))
        self.assertFalse(is_unadopted(listing[2], " FACTORY-BUILD "))

    def test_the_snapshot_takes_only_the_adopted_ones_and_an_explicit_list_is_the_persons_choice(self):
        listing = [
            {"number": 1, "title": "a", "body": "", "comments": [], "labels": [], "url": "u"},
            {"number": 2, "title": "b", "body": "", "comments": [], "labels": [{"name": "factory:observed"}], "url": "u"},
            {"number": 3, "title": "c", "body": "", "comments": [], "labels": [{"name": "factory:observed"}, {"name": "factory-build"}], "url": "u"},
        ]

        def fake(args, cwd, **kwargs):
            if args[:3] == ["gh", "issue", "list"]:
                return json.dumps(listing)
            if args[:3] == ["gh", "pr", "list"]:
                return "[]"
            if args[:2] == ["gh", "api"]:
                number = int(args[2].rsplit("/", 1)[1])
                found = next(i for i in listing if i["number"] == number)
                return json.dumps({"state": "open", "pr": False, "labels": [label["name"] for label in found["labels"]]})
            if args[:3] == ["gh", "issue", "view"]:
                return json.dumps(next(i for i in listing if i["number"] == int(args[3])))
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as temp, patch.object(planner, "command", fake):
            issues, _ = planner.snapshot(Path(temp), Path(temp) / "out", 30, "org/name")
            self.assertEqual([i["number"] for i in issues], [1, 3])
            issues, _ = planner.snapshot(Path(temp), Path(temp) / "out2", 30, "org/name", trigger_label="ship it")
            self.assertEqual([i["number"] for i in issues], [1])
            issues, _ = planner.snapshot(Path(temp), Path(temp) / "out3", None, "org/name", numbers=[2])
            self.assertEqual([i["number"] for i in issues], [2])
            written = json.loads((Path(temp) / "out" / "issues.json").read_text())
            self.assertEqual([i["number"] for i in written], [1, 3])

    def test_a_reused_snapshot_is_filtered_too(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            raw = [
                {"number": 1, "title": "a", "body": "", "comments": [], "labels": []},
                {"number": 2, "title": "b", "body": "", "comments": [], "labels": [{"name": "factory:observed"}]},
            ]
            (destination / "issues.json").write_text(json.dumps(raw))
            (destination / "open-prs.json").write_text("[]")
            seen = []

            def stop(repo, dest, issues, prs, *args, **kwargs):
                seen.extend(i["number"] for i in issues)
                raise RuntimeError("stop")

            with patch.object(planner, "run_planner", stop), self.assertRaises(RuntimeError):
                planner.plan(destination, destination, use_snapshot=True)
            self.assertEqual(seen, [1])
            self.assertEqual([i["number"] for i in json.loads((destination / "issues.json").read_text())], [1])


class TriggerTests(unittest.TestCase):
    def test_a_person_applying_the_trigger_label_to_an_observed_issue_asks_for_a_plan(self):
        from factory import trigger

        label = "factory-build"
        self.assertEqual(trigger.classify(trigger.IssueFacts(5, ("factory-build", "factory:observed")), label), "retry")
        self.assertEqual(trigger.classify(trigger.IssueFacts(5, ("factory:observed",)), label), None)
        # Once a job holds it, the origin label does not make it a retry: it is in flight.
        self.assertEqual(
            trigger.classify(trigger.IssueFacts(5, ("factory-build", "factory:observed", "factory:planned")), label), "in-flight"
        )
        self.assertEqual(
            trigger.classify(trigger.IssueFacts(5, ("factory-build", "factory:observed", "factory:blocked")), label), "retry"
        )
        self.assertEqual(trigger.classify(trigger.IssueFacts(5, ("factory-build", "factory:observed", "factory-ignore")), label), "ignored")
        self.assertEqual(trigger.classify(trigger.IssueFacts(5, ("factory-build",)), label), "eligible")


class CleanObservationTests(unittest.TestCase):
    def test_it_reports_why(self):
        self.assertEqual(clean_observation(5, "build")[1], "shape")
        self.assertEqual(clean_observation({**observation(), "kind": ["x"]}, "build")[1], "kind")
        self.assertIsNone(clean_observation(observation(), "build")[1])


if __name__ == "__main__":
    unittest.main()
