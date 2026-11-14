"""The wiring of .github/workflows/ci.yml, read as text (the repository has no YAML parser).

The checks themselves are the unit tests, the broker tests and the infra tests that the workflow runs. What only the
workflow file can get wrong is pinned here: a job that is missing from the aggregator's `needs:`, an action that is not
pinned to a commit, a secret or a cloud permission that crept into a workflow meant to run for anyone, a Python version
below the package's minimum, a command that names a file that no longer exists, an expression interpolated into a script.
The `CI result` job is the one check docs/handoff.md tells an operator to require, so its name and its `if: always()`
are pinned too (tests/test_handoff_files.py reads the same name in the documents). The real `run:` step of `CI result`
is run with bash for each combination of results, so the verdict is shown, not only read. What could neutralise that
step without touching its script is pinned as well: the exact keys of the job, of its one step and of the workflow, and
no `continue-on-error`, `shell`, `working-directory` or `defaults` anywhere in the file.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RAW = WORKFLOW.read_text(encoding="utf-8")

JOBS = {"compile", "unit-tests", "token-broker", "infra-checks", "ci-result"}
ALLOWED_ACTIONS = {"actions/checkout", "actions/setup-python"}


def code_lines(raw=RAW):
    """The workflow without comment-only lines."""
    return [line for line in raw.splitlines() if not line.lstrip().startswith("#")]


def jobs(raw=RAW):
    """{job id: the lines of its block}."""
    lines = code_lines(raw)
    found, current = {}, None
    for line in lines[lines.index("jobs:") + 1 :]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return found


def field(block, key):
    """The value of a job-level ``key: value`` line (four spaces deep), or None."""
    for line in block:
        match = re.match(rf"^    {re.escape(key)}:\s*(.*?)\s*$", line)
        if match:
            return match.group(1)
    return None


def top_level_keys(raw=RAW):
    """The keys of the workflow itself (column 0)."""
    return sorted(re.findall(r"(?m)^([A-Za-z0-9_-]+):", "\n".join(code_lines(raw))))


def keys_at(block, indent):
    """The mapping keys at exactly this indentation, in the order of the file."""
    return re.findall(rf"(?m)^ {{{indent}}}([A-Za-z0-9_-]+):", "\n".join(block))


def steps(block):
    """The steps of a job, each as the list of its keys; `- key:` opens a step (six spaces), its other keys sit at eight."""
    found = []
    for line in block[block.index("    steps:") + 1 :]:
        opening = re.match(r"^      - ([A-Za-z0-9_-]+):", line)
        other = re.match(r"^        ([A-Za-z0-9_-]+):", line)
        if opening:
            found.append([opening.group(1)])
        elif other and found:
            found[-1].append(other.group(1))
    return found


def run_blocks(raw=RAW):
    """The text of every ``run:`` of the file, single line or block."""
    blocks, current, indent = [], None, 0
    for line in code_lines(raw):
        stripped = line.strip()
        if current is not None:
            if stripped and len(line) - len(line.lstrip()) <= indent:
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
                continue
        if re.match(r"^(- )?run:\s*[|>]", stripped):
            current, indent = [], len(line) - len(line.lstrip())
        elif re.match(r"^(- )?run:\s*\S", stripped):
            blocks.append(stripped)
    if current is not None:
        blocks.append("\n".join(current))
    return blocks


def matrix(block):
    found = re.search(r'^        python: \[(.*?)\]\s*$', "\n".join(block), re.M)
    return re.findall(r'"([0-9.]+)"', found.group(1)) if found else None


class TriggerAndPermissionTests(unittest.TestCase):
    def test_it_runs_on_pull_requests_and_pushes_to_main_and_by_hand_with_no_path_filter(self):
        code = "\n".join(code_lines())
        self.assertRegex(
            code,
            r"(?m)^on:\n  pull_request:\n    branches: \[main\]\n  push:\n    branches: \[main\]\n  workflow_dispatch:\n(?=\npermissions:)",
        )
        # a workflow that a filter keeps from starting leaves its checks "expected" forever (docs/handoff.md)
        self.assertNotRegex(code, r"(?m)^\s*(paths|paths-ignore|branches-ignore|tags)\s*:")
        self.assertNotIn("pull_request_target", code)
        self.assertNotIn("workflow_run", code)

    def test_a_newer_push_to_a_pull_request_cancels_the_older_run_but_a_run_on_main_finishes(self):
        code = "\n".join(code_lines())
        self.assertRegex(
            code,
            r"(?m)^concurrency:\n  group: ci-\$\{\{ github\.workflow \}\}-\$\{\{ github\.event\.pull_request\.number \|\| github\.ref \}\}\n"
            r"  cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}\n",
        )

    def test_it_needs_no_secret_no_variable_and_no_cloud_identity(self):
        code = "\n".join(code_lines())
        self.assertRegex(code, r"(?m)^permissions:\n  contents: read\n\n")
        for forbidden in ("secrets.", "vars.", "id-token", "azure/login", "environment:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code)

    def test_the_workflow_has_exactly_these_top_level_keys_and_one_environment_variable(self):
        # a `defaults:` block (a `run.shell` that swallows every failure) or a `BASH_ENV` would change every step of every job
        self.assertEqual(top_level_keys(), sorted(["name", "on", "permissions", "concurrency", "env", "jobs"]))
        self.assertRegex("\n".join(code_lines()), r'(?m)^env:\n  PYTHONDONTWRITEBYTECODE: "1"\n\njobs:$')

    def test_nothing_changes_what_a_failed_step_means_or_the_shell_that_runs_it(self):
        # `continue-on-error` turns a red step green; `shell` and `defaults` replace the bash that `set -e` and the exit code rely on
        for number, line in enumerate(code_lines(), 1):
            with self.subTest(line=number):
                self.assertNotRegex(line, r"(?<![\w-])(continue-on-error|shell|working-directory|defaults)\s*:")
        for line in RAW.splitlines():  # the pin as written for the whole file, comments included
            self.assertIsNone(re.match(r"^\s*(continue-on-error|shell|working-directory|defaults)\s*:", line), line)

    def test_every_action_is_one_of_two_and_pinned_to_a_commit(self):
        uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", "\n".join(code_lines()), re.M)
        self.assertGreaterEqual(len(uses), 8)
        for use in uses:
            with self.subTest(use=use):
                self.assertRegex(use, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
                self.assertIn(use.split("@")[0], ALLOWED_ACTIONS)

    def test_nothing_is_interpolated_into_a_script(self):
        blocks = run_blocks()
        self.assertGreaterEqual(len(blocks), 8)  # the extractor found the scripts
        for block in blocks:
            self.assertNotIn("${{", block)

    def test_every_job_has_a_timeout_and_the_checkouts_keep_no_credentials(self):
        for name, block in jobs().items():
            with self.subTest(job=name):
                self.assertIsNotNone(field(block, "timeout-minutes"))
                self.assertEqual(field(block, "runs-on"), "ubuntu-latest")
        self.assertEqual(RAW.count("actions/checkout@"), RAW.count("persist-credentials: false"))


class JobTests(unittest.TestCase):
    def test_the_jobs_are_exactly_these(self):
        self.assertEqual(set(jobs()), JOBS)

    def test_python_versions_start_at_the_package_minimum_and_agree(self):
        minimum = re.search(r'(?m)^requires-python = ">=([0-9.]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        found = jobs()
        unit, compiled = matrix(found["unit-tests"]), matrix(found["compile"])
        self.assertEqual(unit, compiled)
        self.assertEqual(unit[0], minimum)
        self.assertEqual(unit, sorted(unit, key=lambda v: [int(part) for part in v.split(".")]))
        self.assertGreaterEqual(len(unit), 2)

    def test_the_commands_are_the_ones_the_documents_give(self):
        found = {name: "\n".join(block) for name, block in jobs().items()}
        self.assertIn("python -m compileall -q src tests tools", found["compile"])
        self.assertIn("python -m json.tool managed-settings.json", found["compile"])
        self.assertIn("python -m json.tool handoff/ruleset.json", found["compile"])
        self.assertIn("pip install -e .", found["unit-tests"])
        self.assertIn("python -m unittest discover -s tests -v", found["unit-tests"])
        self.assertIn("-r tools/requirements-token-broker.txt", found["token-broker"])
        self.assertIn("-m unittest tests.test_token_broker tests.test_github_auth -v", found["token-broker"])
        self.assertIn('grep -q "skipped"', found["token-broker"])  # a skipped broker test is a failure
        self.assertIn("python3 -I infra/test/check_refs.py", found["infra-checks"])
        self.assertIn("python3 -I -m unittest discover -s infra/test", found["infra-checks"])

    def test_the_unit_test_job_installs_a_setuptools_new_enough_before_the_package(self):
        # tests/test_webui.py builds the package with the ambient setuptools: it needs the floor of pyproject.toml, and it
        # fails (it does not skip) when CI is set, so this step is what keeps that test running on every Python version
        floor = re.search(r'(?m)^requires = \["setuptools>=(\d+)"\]$', (ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)
        unit = "\n".join(jobs()["unit-tests"])
        upgrade = f'python -m pip install --upgrade "setuptools>={floor}"'
        self.assertEqual(unit.count(upgrade), 1)
        self.assertLess(unit.index(upgrade), unit.index("pip install -e ."))
        self.assertLess(unit.index(upgrade), unit.index("python -m unittest discover -s tests -v"))
        self.assertEqual(
            re.findall(r"(?m)^      - name: (.+)$", unit),
            ["Check out", "Set up Python", "Install a recent setuptools", "Install package", "Show tool versions", "Run unit tests"],
        )

    def test_every_path_the_workflow_names_exists(self):
        for relative in (
            "src",
            "tests",
            "tools",
            "managed-settings.json",
            "handoff/ruleset.json",
            "tools/requirements-token-broker.txt",
            "tests/test_token_broker.py",
            "tests/test_github_auth.py",
            "infra/test/check_refs.py",
        ):
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).exists(), relative)


class AggregatorTests(unittest.TestCase):
    def setUp(self):
        self.all = jobs()
        self.block = self.all["ci-result"]

    def test_it_is_named_ci_result_and_always_runs(self):
        # the name the documents tell an operator to require; tests/test_handoff_files.py checks the documents
        self.assertEqual(field(self.block, "name"), "CI result")
        self.assertEqual(field(self.block, "if"), "always()")

    def test_it_needs_every_other_job(self):
        needs = field(self.block, "needs")
        self.assertRegex(needs, r"^\[[^\]]+\]$")
        listed = [name.strip() for name in needs.strip("[]").split(",")]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), set(self.all) - {"ci-result"})

    def test_the_job_has_exactly_these_keys_and_one_step_of_a_name_and_a_script(self):
        # `continue-on-error` or `if: false` or a `shell:` on the step, or `continue-on-error` on the job, would keep a red
        # result from ever reaching the check that is required; none of them changes the script that the other tests run
        self.assertEqual(
            sorted(keys_at(self.block, 4)),
            sorted(["name", "if", "needs", "runs-on", "timeout-minutes", "env", "steps"]),
        )
        self.assertEqual(steps(self.block), [["name", "run"]])
        self.assertEqual(sum(1 for line in self.block if re.match(r"^      -", line)), 1)  # also a step in flow style
        self.assertEqual(keys_at(self.block, 6), ["NEEDS_RESULTS"])  # the job's env: one variable, and nothing else
        self.assertEqual(len(run_blocks("jobs:\n  ci-result:\n" + "\n".join(self.block))), 1)

    def test_the_results_reach_the_script_through_env_only(self):
        self.assertRegex("\n".join(self.block), r"(?m)^    env:\n      NEEDS_RESULTS: \$\{\{ join\(needs\.\*\.result, ' '\) \}\}$")
        script = run_blocks("jobs:\n  ci-result:\n" + "\n".join(self.block))[0]
        self.assertIn("$NEEDS_RESULTS", script)
        self.assertNotIn("needs.", script)

    def verdict(self, results):
        script = run_blocks("jobs:\n  ci-result:\n" + "\n".join(self.block))[0]
        done = subprocess.run(["bash", "-e", "-c", script], env={"PATH": "/usr/bin:/bin", "NEEDS_RESULTS": results}, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout

    @unittest.skipUnless(shutil.which("bash"), "needs bash, the shell of the workflow's run steps")
    def test_only_all_success_is_green(self):
        self.assertEqual(self.verdict("success success success success")[0], 0)
        for results in (
            "success success success failure",
            "failure success success success",
            "success cancelled success success",
            "success success skipped success",
        ):
            with self.subTest(results=results):
                code, out = self.verdict(results)
                self.assertEqual(code, 1, out)
                self.assertIn("::error::a needed job ended as", out)


if __name__ == "__main__":
    unittest.main()
