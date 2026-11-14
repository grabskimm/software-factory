"""The Claude Code skill of this repository (.claude/skills/factory/SKILL.md) is pinned against the CLI and the code.

The skill is plain text that a person or an agent follows, and, when this repository is itself a Factory target, a
worker session reads it too (``agent.repo_skills``). Nothing runs it, so without these tests it would drift silently: a
renamed flag, a new subcommand, a changed status value or default would leave an operator following stale instructions.
Each test below reads one kind of claim out of the skill and checks it against the real parser, the real constants, the
real behaviour of a public function or the real output of ``factory status --json``. Names are matched as whole tokens
(never as a substring of a longer name) and every complete list the skill states is compared for equality.
"""

import argparse
import contextlib
import inspect
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import factory
from factory import agent, blockers, cli, completion, config, control, epic, followup_serve, fragments, github
from factory import issue_comments, issue_labels, model_choice, operations, phases, preflight, reason_codes
from factory import recovery, schemas, stopworld
from factory.config import RepoConfig

ROOT = Path(__file__).resolve().parent.parent  # the checkout this test file is in, whatever ``factory`` resolves to
SRC = Path(factory.__file__).resolve().parent  # the package source: used only to grep it
SKILL = ROOT / ".claude" / "skills" / "factory" / "SKILL.md"

# The size guard. The bytes are what a repository pays in every session's prompt when it ships this skill (about 3k
# tokens); the width keeps the paragraphs readable (the file is wrapped near 120 columns, tables and fenced examples
# included); the lines are a loose bound on both.
MAX_BYTES = 14_000
MAX_LINES = 160
MAX_WIDTH = 130
DESCRIPTION_LIMIT = 300  # ``agent._skill_text`` keeps this many characters of a skill's description

# Subcommands the skill shows only inside a sentence, not as a line of a fenced block, each with the reason.
PROSE_ONLY = {
    "preflight": "a probe with its own paragraph (it has no fenced example because it spends a real session)",
    "record-capabilities": "named with preflight, whose results it records",
    "followup-release": "what the manager runs, not what an operator types",
}
# A word in backticks that looks like a label but is not one: the name of the manager process.
NOT_LABELS = {"factory-manager"}
# The two forms of the skill's shell function `f`, as the global options each puts before a subcommand.
MANAGED = ["--repo", "ORG/NAME", "--run-dir", "/data/repos/ORG/NAME/runs/RUN"]
LOCAL = ["--run-dir", "factory/runs/RUN"]

# The key of a capability record that the skill names (the other words it names are config keys, statuses, keys of the
# status report, keys of a plan job or keys of a verification record).
RECORD_KEYS = {"tested_at"}

FENCE = re.compile(r"^```[a-z]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
INLINE = re.compile(r"`([^`\n]+)`")
FUNCTION = re.compile(r'f\(\) \{ factory (?P<options>.*) "\$@"; \}')

PLAN = {
    "version": 1,
    "issues": [{"number": 12, "title": "first"}, {"number": 13, "title": "second"}],
    "jobs": [
        {
            "id": "fix-a",
            "objective": "o",
            "acceptance": ["a works"],
            "issues": [12],
            "likely_paths": ["src/a.py"],
            "feature": True,
            "depends_on": [],
        },
        {
            "id": "fix-b",
            "objective": "o",
            "acceptance": ["b works"],
            "issues": [13],
            "likely_paths": ["src/b.py"],
            "feature": True,
            "depends_on": [],
        },
    ],
    "dispositions": [
        {"issue": 12, "status": "job", "job": "fix-a", "reason": "r"},
        {"issue": 13, "status": "job", "job": "fix-b", "reason": "r"},
    ],
}
STATE = {
    "jobs": {
        "fix-a": {"status": "queued"},
        "fix-b": {"status": "pr_ready", "phase": "publish", "pr": "https://example.org/x/y/pull/1"},
    }
}


def read_skill():
    return SKILL.read_text(encoding="utf-8")


def flat(text):
    """``text`` on one line: a claim of the skill must not depend on where a paragraph is wrapped."""
    return " ".join(text.split())


def code_of(text):
    """The text of every fenced block and every inline code span, one per line."""
    prose = FENCE.sub("", text)
    return "\n".join([*FENCE.findall(text), *INLINE.findall(prose)])


def words(fragment):
    """The backticked lowercase words (with ``_`` or ``-``) of a fragment of the skill."""
    return set(re.findall(r"`([a-z][a-z0-9_-]*)`", fragment))


def token(name):
    """A pattern for ``name`` as a whole token: not part of a longer word, name or option."""
    return rf"(?<![\w-]){re.escape(name)}(?![\w-])"


def claim(pattern, text, group="v"):
    """The named group of the one sentence of the skill that matches ``pattern``; a failure says which one."""
    found = re.search(pattern, flat(text), re.DOTALL)
    if found is None:
        raise AssertionError(f"SKILL.md no longer has the sentence matching {pattern!r}; update this test with it")
    return found.group(group)


def parser_facts():
    """What ``cli.build_parser()`` offers: its subcommands and, per subcommand, each long option with its choices.

    argparse has no public way to list the subcommands or the options of a parser, so this is the one place of the
    module that reads its private attributes (``_actions``, ``_SubParsersAction``); everything else asks this helper.
    """
    parser = cli.build_parser()

    def options_of(one):
        return {
            option: action.choices
            for action in one._actions
            for option in action.option_strings
            if option.startswith("--")
        }

    commands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands.update({name: options_of(sub) for name, sub in action.choices.items()})
    every = {*options_of(parser), *(option for options in commands.values() for option in options)}
    return {"global": options_of(parser), "commands": commands, "options": every}


def subcommands():
    return list(parser_facts()["commands"])


def parse(argv):
    """The namespace of ``argv`` for the real parser; the test fails with the parser's own words when it refuses."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return cli.build_parser().parse_args(argv)
        except SystemExit:
            raise AssertionError(f"the parser refuses {argv!r}: {err.getvalue().strip().splitlines()[-1:]}") from None


def refusals(args):
    """What the CLI refuses after it parsed ``args``, for the commands the skill shows (the parser alone accepts them)."""
    try:
        if args.command == "purge-clone":
            cli.check_purge_clone_args(args)  # --repo yes, --run-dir no
        if args.command == "plan" and args.issues is not None and args.limit is not None:
            raise ValueError("--issues and --limit cannot be combined")
    except ValueError as error:
        raise AssertionError(f"the CLI refuses {args}: {error}") from None
    return args


def fenced_commands(text):
    """``(line, subcommand)`` of every command line of every fenced block, parsed as the managed and the local form."""
    found = []
    for block in FENCE.findall(text):
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or FUNCTION.match(line):
                continue
            tokens = shlex.split(line, comments=True)
            if tokens[0] == "f":  # the function of the skill: both of its forms must accept the command
                refusals(parse([*LOCAL, *tokens[1:]]))
                found.append((line, refusals(parse([*MANAGED, *tokens[1:]])).command))
            else:
                assert tokens[0] == "factory", line
                found.append((line, refusals(parse(tokens[1:])).command))
    return found


def command_spans(text):
    """``(span, argv)`` of every inline code span of the prose that is a command line of a subcommand with an argument.

    A span such as ``resume JOB --phase publish`` or ``f plan --issues 20 --append-to RUN``; an alternative
    (``build|verify``) is parsed as its first value. A bare name is a mention, not a command line.
    """
    names = set(subcommands())
    found = []
    for span in sorted(set(INLINE.findall(FENCE.sub("", text)))):
        shown = re.sub(r"([a-z]+)(?:\|[a-z]+)+", r"\1", span)
        try:
            tokens = shlex.split(shown.removeprefix("f "))
        except ValueError:  # a span that is prose, not a command line (an apostrophe, a lone quote)
            continue
        if len(tokens) >= 2 and tokens[0] in names and not any(mark in shown for mark in "<>[{"):
            found.append((span, tokens))
    return found


def source_text():
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(SRC.glob("*.py")))


def run_status(directory, *flags):
    """``factory --run-dir DIR status FLAGS`` in local mode, in process, with no FACTORY_* variable set."""
    args = parse(["--run-dir", str(directory), "status", *flags])
    out = io.StringIO()
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out):
        for name in [name for name in os.environ if name.startswith("FACTORY_")]:
            del os.environ[name]
        cli.run(args)
    return out.getvalue()


def status_json(directory, state=STATE):
    """``status --json --offline`` of a synthetic run of two jobs, as a dict."""
    directory.mkdir()
    (directory / "plan.json").write_text(json.dumps(PLAN))
    (directory / "state.json").write_text(json.dumps(state))
    return json.loads(run_status(directory, "--json", "--offline"))


def waiting_phases():
    """The phases of the ``waiting_<phase>`` statuses, read from three places of the code that must agree."""
    queued = set(re.findall(r'self\.queue_phase\(key, "(\w+)"\)', (SRC / "runner.py").read_text(encoding="utf-8")))
    labelled = set(issue_labels.PHASE_LABELS)
    served = {status.removeprefix("waiting_") for status in followup_serve.WAITING_FOR_RUN if status.startswith("waiting_")}
    if queued != labelled or labelled != served:
        raise AssertionError(f"the code disagrees about the waiting phases: {queued} {labelled} {served}")
    return labelled


class FakeState:
    """The little a ``blockers`` function needs of a ``State``: the jobs, a mutex and a save."""

    def __init__(self):
        self.data = {"jobs": {"fix-a": {}}}
        self.mutex = threading.Lock()

    def save(self):
        pass


class FrontMatterTests(unittest.TestCase):
    def test_the_front_matter_is_a_name_and_a_description_in_plain_yaml(self):
        text = read_skill()
        self.assertTrue(text.startswith("---\n"))
        front, separator, body = text[4:].partition("\n---\n")
        self.assertEqual(separator, "\n---\n", "the front matter is not closed")
        pairs = {}
        for line in front.splitlines():
            key, colon, value = line.partition(": ")
            self.assertEqual(colon, ": ", f"not a plain 'key: value' line: {line!r}")
            pairs[key] = value
        self.assertEqual(sorted(pairs), ["description", "name"])
        self.assertEqual(pairs["name"], "factory")
        description = pairs["description"]
        # A plain YAML scalar: no ': ' or ' #' inside, not quoted or a block, on one line, with no tab.
        self.assertNotIn(": ", description)
        self.assertNotIn(" #", description)
        self.assertNotIn("\t", description)
        self.assertNotIn(description[0], "\"'>|[{&*!%@`")
        self.assertNotIn(description[-1], "\"'")
        # It says what the skill does and when to use it, and fits the characters the worker prompt keeps.
        self.assertRegex(description, r"^Operate Factory\b")
        self.assertRegex(description, r"\bUse (to|when|for)\b")
        self.assertLessEqual(len(description), DESCRIPTION_LIMIT)
        self.assertTrue(body.strip())

    def test_the_description_names_the_cli_verbs_and_the_label_driven_path(self):
        description = re.search(r"(?m)^description: (.*)$", read_skill()).group(1)
        verbs = {verb.strip() for verb in claim(r"through its CLI \((?P<v>[^)]*)\)", description).split(",")}
        self.assertGreaterEqual(len(verbs), 8)
        self.assertLessEqual(verbs, set(subcommands()))
        # A request about an issue ("why is my factory-build issue blocked") must find the skill too.
        self.assertRegex(description, token("factory-build"))
        self.assertIn("factory:*", description)
        self.assertRegex(description, r"\bblocked\b")
        self.assertIn(github.GATE_CONTEXT, description)

    def test_a_worker_prompt_reads_the_whole_skill_with_the_description_of_the_file(self):
        text = read_skill()
        description = re.search(r"(?m)^description: (.*)$", text).group(1)
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            target = repo / ".claude" / "skills" / "factory" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text(text, encoding="utf-8")
            for args in (
                ["init", "-q", "-b", "main"],
                ["config", "user.email", "t@example.invalid"],
                ["config", "user.name", "T"],
                ["add", "."],
                ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "skill"],
            ):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            prompt = agent.repo_skills(repo, "HEAD")
        body = text.partition("\n---\n")[2].strip()
        self.assertIn(f"### Skill: factory\n{description}\n\n# Factory operator", prompt)
        self.assertIn(body, prompt, "the worker prompt does not carry the whole skill")
        self.assertNotIn("not included in full", prompt)
        self.assertLessEqual(len(text.encode("utf-8")), agent.MAX_SKILL_BYTES)

    def test_it_stays_short_and_has_nothing_unfinished_in_it(self):
        raw = SKILL.read_bytes()
        text = raw.decode("utf-8")
        self.assertLessEqual(len(raw), MAX_BYTES)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), MAX_LINES)
        for number, line in enumerate(lines, 1):
            if line.startswith("description: "):  # one plain YAML line, bounded by DESCRIPTION_LIMIT instead
                continue
            self.assertLessEqual(len(line), MAX_WIDTH, f"line {number} is {len(line)} characters wide")
        for marker in ("TODO", "FIXME"):
            self.assertNotIn(marker, text)
        self.assertNotRegex(text, r"#\d+", "the skill names no issue or pull request number")

    def test_it_says_how_local_and_managed_mode_differ(self):
        text = flat(read_skill())
        for needle in (
            "Local mode (no `--repo`)",
            "Managed mode (`--repo ORG/NAME`)",
            "`FACTORY_CONFIG` and `FACTORY_DATA` are required, with no defaults",
            "unknown keys are errors",
            "`--run-dir` is required, except that `purge-clone` takes none and `plan --append-to RUN` derives it from RUN",
        ):
            self.assertIn(needle, text)
        # Local mode: the run directory it names is the one the CLI uses.
        directory, _ = cli.resolve_run_dir(argparse.Namespace(run_dir=None), RepoConfig())
        self.assertEqual(directory.parts[-3:], ("factory", "runs", "pilot"))
        self.assertIn("`factory/runs/pilot`", text)
        # Managed mode: the paths it names, the two variables it requires and the one exception to --run-dir.
        cfg = RepoConfig(repo="org/name")
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"FACTORY_DATA": temp}):
            self.assertEqual(config.data_paths(cfg).root, Path(temp).resolve() / "repos" / "org" / "name")
            with self.assertRaisesRegex(ValueError, "--run-dir is required"):
                cli.resolve_run_dir(argparse.Namespace(run_dir=None), cfg)
            derived, _ = cli.resolve_run_dir(argparse.Namespace(run_dir=None, append_to="RUN"), cfg)
            self.assertEqual(derived, config.data_paths(cfg).run_dir("RUN"))
            cli.check_purge_clone_args(argparse.Namespace(repo="org/name", run_dir=None))
            with self.assertRaisesRegex(ValueError, "takes no --run-dir"):
                cli.check_purge_clone_args(argparse.Namespace(repo="org/name", run_dir=Path("runs/RUN")))
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "repos").mkdir()
            (Path(temp) / "repos" / "org__name.toml").write_text('repo = "org/name"\nunknown_key = 1\n')
            with self.assertRaises(ValueError):  # unknown keys are errors
                config.load_repo_config("org/name", Path(temp), env={})
            (Path(temp) / "repos" / "org__name.toml").write_text('repo = "org/name"\n')
            self.assertEqual(config.load_repo_config("org/name", Path(temp), env={}).repo, "org/name")
        with self.assertRaisesRegex(ValueError, "FACTORY_CONFIG"):
            config.load_repo_config("org/name", env={})
        with self.assertRaisesRegex(ValueError, "FACTORY_DATA"):
            config.data_paths(cfg, env={})

    def test_the_prerequisites_it_names_are_those_of_the_package(self):
        text = read_skill()
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        version = claim(r"Needs Python (?P<v>\d+\.\d+)\+", text)
        self.assertEqual(f">={version}", project["requires-python"])
        for name in ("factory", "factory-manager"):
            self.assertIn(name, project["scripts"], name)
        self.assertIn("python -m factory", text)
        self.assertTrue((SRC / "__main__.py").is_file())

    def test_the_documents_it_points_to_exist(self):
        text = read_skill()
        pointers = set(re.findall(r"`(docs/[A-Za-z0-9._-]+\.md|[a-z][a-z-]*/)`", text))  # a document, or a directory
        self.assertTrue(
            {
                "docs/configuration.md",
                "docs/operations.md",
                "docs/manager.md",
                "docs/handoff.md",
                "docs/capacity.md",
                "docs/pod-backend.md",
                "handoff/",
            }
            <= pointers
        )
        for pointer in pointers:
            self.assertTrue((ROOT / pointer).exists(), pointer)
        self.assertIn("Jobs that change `.github/workflows`", flat(text))
        self.assertIn("Jobs that change `.github/workflows`", (ROOT / "docs" / "operations.md").read_text(encoding="utf-8"))


class CommandTests(unittest.TestCase):
    def test_every_subcommand_of_the_cli_is_in_a_fenced_example_or_listed_as_prose_only(self):
        text = read_skill()
        names = subcommands()
        self.assertGreaterEqual(len(names), 17)
        shown = {command for _, command in fenced_commands(text)}
        self.assertEqual(set(names) - shown, set(PROSE_ONLY), "a subcommand lost its line in a fenced block")
        self.assertLessEqual(shown, set(names))
        code = code_of(text)
        for name, reason in PROSE_ONLY.items():
            self.assertTrue(reason, f"say why {name} has no example")
            self.assertRegex(code, token(name), f"the skill does not mention `{name}`")

    def test_the_function_of_the_skill_has_the_two_forms_it_describes(self):
        text = read_skill()
        forms = [shlex.split(match.group("options").replace("$FACTORY_DATA", "/data")) for match in FUNCTION.finditer(text)]
        self.assertEqual(forms, [MANAGED, LOCAL])
        for form in forms:
            self.assertEqual(parse([*form, "status"]).command, "status")
        self.assertIsNotNone(parse([*MANAGED, "status"]).repo)
        self.assertIsNone(parse([*LOCAL, "status"]).repo)
        self.assertIn("not a variable, so it also works in zsh", flat(text))
        self.assertNotRegex(text, r"\$F\b", "the old `$F` idiom relies on word splitting, which zsh does not do")

    def test_every_command_in_a_code_span_parses(self):
        spans = command_spans(read_skill())
        self.assertGreaterEqual(len(spans), 8)
        shown = {refusals(parse([*MANAGED, *tokens])).command for _, tokens in spans}
        self.assertLessEqual(set(PROSE_ONLY), shown, "a subcommand that has no fenced example has no usage example at all")

    def test_every_option_the_skill_names_exists_in_the_parser(self):
        text = read_skill()
        named = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))
        self.assertTrue(
            {"--repo", "--run-dir", "--issues", "--append-to", "--offline", "--followup", "--phase", "--plan-file"} <= named
        )
        self.assertEqual(named - parser_facts()["options"], set())
        # The two global options go before the subcommand, as the skill says: they belong to the parser, not to a command.
        self.assertTrue({"--repo", "--run-dir"} <= set(parser_facts()["global"]))
        for command in parser_facts()["commands"].values():
            self.assertFalse({"--repo", "--run-dir"} & set(command))

    def test_the_choices_the_skill_lists_are_the_choices_of_the_cli(self):
        text = read_skill()
        commands = parser_facts()["commands"]
        resume = claim(r"# (?P<v>[a-z]+(?:\|[a-z]+)+) from a saved checkpoint", text).split("|")
        self.assertEqual(resume, list(commands["resume"]["--phase"]))
        probe = claim(r"`preflight JOB --phase (?P<v>[a-z]+(?:\|[a-z]+)+)`", text).split("|")
        self.assertEqual(probe, list(commands["preflight"]["--phase"]))
        rounds = commands["run"]["--review-rounds"]
        low, high = claim(r"--review-rounds (?P<v>\d)-\d", text), claim(r"--review-rounds \d-(?P<v>\d)", text)
        self.assertEqual(list(range(int(low), int(high) + 1)), sorted(rounds))
        budgets = claim(r"`--budget-(?P<v>[a-z]+(?:\|[a-z]+)+) USD`", text).split("|")
        self.assertEqual({f"--budget-{name}" for name in budgets}, {o for o in commands["run"] if o.startswith("--budget-")})
        self.assertIn("--timeout", commands["run"])

    def test_the_numbers_and_defaults_it_states_are_those_of_the_code(self):
        text = read_skill()
        self.assertEqual(int(claim(r"has (?P<v>\d+) jobs", text)), fragments.MAX_RUN_JOBS)
        self.assertEqual(int(claim(r"FACTORY_STOP_GRACE_SECONDS, (?P<v>\d+)\)", text)), int(control.DEFAULT_STOP_GRACE))
        self.assertEqual(float(claim(r"default \$(?P<v>\d+\.\d+)\)", text)), phases.POLICIES["preflight"].budget_usd)
        self.assertRegex(text, token(control.ENV_STOP_GRACE))
        self.assertEqual(int(claim(r"plan --limit (?P<v>\d+)", text)), cli.DEFAULT_PLAN_LIMIT)
        run = parse(["run"])
        self.assertEqual(int(claim(r"run --concurrency (?P<v>\d+)", text)), run.concurrency)
        self.assertEqual(int(claim(r"--verification-concurrency (?P<v>\d+)", text)), run.verification_concurrency)
        # A skill over this size, or past this budget, is listed and not included.
        self.assertEqual(int(claim(r"A skill over (?P<v>\d+) KiB", text)) * 1024, agent.MAX_SKILL_BYTES)
        budget = int(claim(r"`repo_skills_budget_kib` \(default (?P<v>\d+) KiB\)", text))
        self.assertEqual(budget, RepoConfig().repo_skills_budget_kib)
        self.assertEqual(budget, agent.DEFAULT_SKILLS_BUDGET_KIB)

    def test_the_exit_code_of_run_and_the_return_of_drain_are_those_of_the_code(self):
        text = read_skill()
        source = inspect.getsource(cli.run_command)
        # After a drain `run` returns (exit 0) whatever the jobs say; only then does it exit non-zero for unfinished jobs.
        drained_first = r"if runner\.drained:.*?\n\s+return\n"
        found = re.search(drained_first + r".*?not in \{([^}]*)\}.*?raise SystemExit\((\d+)\)", source, re.S)
        self.assertIsNotNone(found, "run_command no longer returns after a drain before it decides the exit code")
        self.assertEqual(int(claim(r"`run` exits (?P<v>\d+) while any job is not", text)), int(found.group(2)))
        finished = words(claim(r"is not (?P<v>`pr_ready`, `merged` or `closed`) \(", text))
        self.assertEqual(finished, set(re.findall(r'"(\w+)"', found.group(1))))
        self.assertIn("(0 after a drain)", flat(text))
        # `drain` only asks the supervisor and returns at once: it takes no lock and starts nothing.
        until_lock = r'args\.command == "drain":(.*?)\n    # One supervisor per repository'
        branch = re.search(until_lock, inspect.getsource(cli.run), re.S)
        self.assertIsNotNone(branch, "cli.run no longer answers drain before it takes the repository lock")
        self.assertIn("request_drain(", branch.group(1))
        self.assertRegex(branch.group(1), r"\n\s+return\n")
        self.assertIn("(returns at once)", text)


class VocabularyTests(unittest.TestCase):
    def test_the_job_statuses_are_exactly_the_job_statuses_of_the_code(self):
        text = read_skill()
        listed = words(claim(r"Job `status` is one of (?P<v>.*?)\. A job that waits", text))
        self.assertEqual(listed, set(operations.JOB_STATUSES))
        self.assertLessEqual(set(issue_labels.JOB_STATUS_LABELS), listed)  # every status that has a label is listed
        self.assertTrue({"queued", "running", "pr_ready", "pr_incomplete"} <= listed)

    def test_the_waiting_statuses_are_the_phases_the_runner_queues_and_retry_refuses_them(self):
        text = read_skill()
        phases_waiting = waiting_phases()
        self.assertEqual(phases_waiting, {"build", "review", "verify", "publish"})
        waiting = words(claim(r"A job that waits for a worker slot of its phase is (?P<v>.*?): `run` continues it", text))
        self.assertEqual(waiting, {f"waiting_{phase}" for phase in phases_waiting})
        self.assertIn('status=f"waiting_{phase}"', (SRC / "runner.py").read_text(encoding="utf-8"))
        self.assertIn("`retry` refuses it", flat(text))
        conditions = re.findall(r"not in \{([^}]*)\}", inspect.getsource(cli.retry_command))
        retried = next(found for found in (set(re.findall(r'"(\w+)"', body)) for body in conditions) if "failed" in found)
        self.assertEqual(retried & waiting, set())
        self.assertEqual(words(claim(r"`retry` is for (?P<v>.*?) jobs;", text)), retried)

    def test_the_handoff_values_are_what_the_code_reports(self):
        sentence = claim(r"(?P<v>`handoff_status` is .*?)`--offline` shows", read_skill())
        # Each value is a backticked word that opens a parenthesis (`merged` / `closed` share one).
        pairs = re.findall(r"`([a-z_]+)`(?: / `([a-z_]+)`)? \(", sentence)
        listed = {value for pair in pairs for value in pair if value}
        failed = {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}
        produced = {
            github.handoff_status({"state": "MERGED"}),
            github.handoff_status({"state": "CLOSED"}),
            github.handoff_status({"state": "OPEN"}),
            github.handoff_status({"state": "OPEN", "statusCheckRollup": [failed]}),
        }
        self.assertEqual(produced, {"published", "ci_failed", "merged", "closed"})
        self.assertEqual(listed, produced | {"null"})

    def test_the_reason_codes_and_capabilities_are_those_of_the_code(self):
        text = read_skill()
        self.assertEqual(words(claim(r"`reason_code` says why \((?P<v>[^)]*)\)", text)), set(reason_codes.ORDER))
        self.assertEqual(words(claim(r"records which of (?P<v>.*?) the sandbox allows", text)), set(preflight.CAPABILITIES))

    def test_the_capability_record_it_describes_is_accepted_by_the_code(self):
        text = read_skill()
        fields = words(claim(r"records a JSON array of such results from elsewhere \((?P<v>[^)]*)\)", text))
        statuses = claim(r"`status` (?P<v>[a-z]+(?:\|[a-z]+)+)", text).split("|")
        self.assertEqual(len(statuses), 3)
        for status in statuses:
            record = {
                "id": "workspace_write",
                "environment": "ci",
                "status": status,
                "evidence": ["log"],
                "tested_at": "2026-01-01T00:00:00Z",
            }
            self.assertEqual(fields, set(record))  # the keys it names are exactly the keys of a record the code accepts
            self.assertEqual(blockers.record_capabilities(FakeState(), [record])[0]["status"], status)
        with self.assertRaises(ValueError):
            blockers.record_capabilities(FakeState(), [{"id": "x", "environment": "ci", "status": "maybe"}])

    def test_the_blocker_and_evidence_records_it_describes_are_those_the_code_requires(self):
        text = read_skill()
        keys = set(re.findall(r'"([a-z_]+)"', claim(r"`blocker\.json` is `(?P<v>\{.*?\})`", text)))
        self.assertEqual(keys, {"id", "affected_jobs", "cause", "evidence", "remedy", "owner"})
        record = {"id": "b1", "affected_jobs": ["fix-a"], "cause": "c", "evidence": ["log"], "remedy": "r", "owner": "o"}
        self.assertEqual(blockers.record_blocker(FakeState(), dict(record))["id"], "b1")
        for key in keys:  # each key the skill names is required, and nothing else is
            partial = {name: value for name, value in record.items() if name != key}
            with self.assertRaises(ValueError, msg=key):
                blockers.record_blocker(FakeState(), partial)
        with self.assertRaises(ValueError):
            blockers.record_blocker(FakeState(), {**record, "extra": 1})
        # evidence.json: the keys are those of a verification record, plus the two of an import.
        shown = set(re.findall(r'"([a-z_]+)"', claim(r"`evidence\.json` is `(?P<v>\{.*?\})`", text)))
        self.assertEqual(shown - {"job_id", "tested_commit"}, {"commit", "complete", "scenario", "environment", "assertions"})
        self.assertLessEqual(shown - {"job_id", "tested_commit"}, set(schemas.VERIFY["properties"]))
        needed = claim(r"With `complete` true, (?P<v>.*?) must be non-empty", text)
        self.assertEqual(words(needed), {"scenario", "environment", "assertions"})
        self.assertIn("`recording_url`", text)
        self.assertIn("recording_url", schemas.VERIFY["properties"])
        self.assertIn("with `complete` false a `blocker` is needed", flat(text))
        self.assertIn("blocker", schemas.VERIFY["properties"])
        code = inspect.getsource(recovery)
        self.assertIn("incomplete verification must record a blocker", code)
        self.assertIn("completed recording-mode evidence requires an HTTPS recording_url", code)
        self.assertIn("imported evidence must identify tested_commit", code)
        self.assertIn("recording", config.ACCEPTANCE_MODES)
        self.assertEqual(claim(r"when `\[acceptance\] mode` is `(?P<v>[a-z]+)`", text), "recording")
        self.assertEqual(claim(r"so must an (?P<v>https) `recording_url`", text), "https")  # the code insists on HTTPS
        with tempfile.TemporaryDirectory() as temp:  # the TOML spelling of that setting
            (Path(temp) / "repos").mkdir()
            (Path(temp) / "repos" / "org__name.toml").write_text(
                'repo = "org/name"\n[acceptance]\nmode = "recording"\n[artifacts]\nroots = ["artifacts"]\n'
            )
            self.assertEqual(config.load_repo_config("org/name", Path(temp), env={}).acceptance_mode, "recording")

    def test_the_dispositions_and_status_labels_are_those_of_the_code(self):
        text = read_skill()
        self.assertEqual(
            words(claim(r"exactly one disposition \((?P<v>[^)]*)\)", text)),
            {"job", *fragments.DISPOSITION_STATUSES},
        )
        short = {name.removeprefix("factory:") for name in issue_labels.STATUS_LABELS}
        self.assertEqual(words(claim(r"`factory:\*`: one status label per issue \((?P<v>.*?)\)\. ", text)), short)

    def test_the_labels_it_names_are_exactly_the_labels_of_the_code(self):
        text = read_skill()
        claimed = set(re.findall(r"`(factory(?:[-:][a-z]+)+)", text)) - NOT_LABELS
        known = {
            config.DEFAULT_TRIGGER_LABEL,
            issue_labels.IGNORE_LABEL,
            epic.LABEL_APPROVE,
            github.LABEL_READY,
            issue_labels.LABEL_OBSERVED,
            completion.COMPLETION_LABEL,
            model_choice.MODEL_LABEL_PREFIX.removesuffix(":"),
            model_choice.EFFORT_LABEL_PREFIX.removesuffix(":"),
            *issue_labels.STATUS_LABELS,
        }
        self.assertEqual(claimed - known, set(), "a label of the skill is not a label of the code")
        # The labels the skill is about are all there (an operator reads this to find them).
        self.assertTrue(
            {"factory-build", "factory-ignore", "factory-approve-plan", "factory:observed", "factory-verified", "factory:ready"}
            <= claimed
        )
        self.assertEqual(config.DEFAULT_TRIGGER_LABEL, RepoConfig().auto_build_label)
        self.assertEqual(github.GATE_CONTEXT, "factory/gate")
        # Every `factory/NAME` of the skill (not a path such as factory/runs/pilot) is the context of the gate.
        self.assertEqual(set(re.findall(r"(?<![\w/-])factory/[a-z][a-z-]*(?![\w/-])", text)), {github.GATE_CONTEXT})
        for name in NOT_LABELS:
            self.assertIn(name, tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"])

    def test_the_effort_labels_and_what_they_apply_to_are_those_of_the_code(self):
        text = read_skill()
        efforts = claim(r"`factory-effort:<(?P<v>[a-z|]+)>`", text).split("|")
        self.assertEqual(efforts, list(phases.EFFORTS))
        applies = claim(r"`factory-effort:<[a-z|]+>`: at most one of each, for (?P<v>\w+ and \w+) only", text)
        self.assertEqual(applies.split(" and "), list(phases.ISSUE_PHASES))
        self.assertIn("`max_effort`", text)
        self.assertIn("max_effort", RepoConfig.__dataclass_fields__)

    def test_the_labels_that_write_and_the_commands_that_do_not_are_those_of_the_code(self):
        text = read_skill()
        flatted = flat(text)
        silent = words(claim(r"and `plan`, `reconcile` and `purge`, writes the issue labels; (?P<v>.*?) write none", text))
        self.assertEqual(silent, {"status", "preflight", "record-blocker", "record-capabilities"})
        self.assertLessEqual(silent | {"drain", "stop", "plan", "reconcile", "purge"}, set(subcommands()))
        self.assertIn("`drain` and `stop` only ask the supervisor", flatted)
        # The commands that change a job or settle labels attach the label observer; stop and drain do not.
        for function in (
            cli.plan_command,
            cli.retry_command,
            cli.recovery_command,  # resume, import-evidence, the record-* commands and preflight share this one
            cli.block_command,
            cli.followup_release_command,
            cli.recover_command,
            cli.purge_command,
            cli.reconcile_command,
        ):
            self.assertIn("IssueLabels", inspect.getsource(function), function.__name__)
        self.assertNotIn("IssueLabels", inspect.getsource(cli.stop_command))

    def test_every_env_variable_and_config_key_it_names_is_real(self):
        text = read_skill()
        source = source_text()
        variables = set(re.findall(r"(?<![\w-])((?:FACTORY|ANTHROPIC|GITHUB)_[A-Z0-9_]*[A-Z0-9])(?![\w-])", text))
        self.assertTrue(
            {
                "ANTHROPIC_API_KEY",
                "FACTORY_CONFIG",
                "FACTORY_DATA",
                "FACTORY_ISSUE_COMMENTS",
                "FACTORY_EPIC_MODE",
                "FACTORY_POD_BACKEND",
                "FACTORY_MANAGER_TRIGGER_SECONDS",
                "FACTORY_GITHUB_APP_ID",
                "FACTORY_TOKEN_BROKER",
                "GITHUB_APP_PRIVATE_KEY",
            }
            <= variables
        )
        for variable in variables:
            self.assertRegex(source, token(variable), variable)
        # `NAME=off` switches: the code reads the very variable and treats the very value as off.
        self.assertEqual(issue_comments.ENV_SWITCH, "FACTORY_ISSUE_COMMENTS")
        self.assertTrue(issue_comments.switched_off({"FACTORY_ISSUE_COMMENTS": "off"}))
        self.assertEqual(epic.ENV_SWITCH, "FACTORY_EPIC_MODE")
        self.assertTrue(epic.switched_off({"FACTORY_EPIC_MODE": "off"}))
        switches = {
            ("FACTORY_ISSUE_COMMENTS", "off"),
            ("FACTORY_EPIC_MODE", "off"),
            ("FACTORY_POD_BACKEND", "on"),
            ("FACTORY_MANAGER_TRIGGER_SECONDS", "0"),
        }
        self.assertEqual(set(re.findall(r"`([A-Z_]+)=([a-z0-9]+)`", text)), switches)

    def test_every_config_key_it_sets_loads_and_the_defaults_it_states_are_those_of_the_code(self):
        text = read_skill()
        # `key = value` spans: a real config key, and every value shown is one the config loader accepts.
        spans = re.findall(r"`([a-z][a-z_]*) = ([^`]*)`", text)
        self.assertGreaterEqual(len(spans), 7)
        for key, rest in spans:
            self.assertIn(key, RepoConfig.__dataclass_fields__, key)
            values = [*re.findall(r'"([^"]*)"', rest), *[word == "true" for word in re.findall(r"\b(true|false)\b", rest)]]
            self.assertTrue(values, f"{key} = {rest}")
            for value in values:
                with tempfile.TemporaryDirectory() as temp:
                    (Path(temp) / "repos").mkdir()
                    shown = json.dumps(value) if isinstance(value, str) else str(value).lower()
                    (Path(temp) / "repos" / "org__name.toml").write_text(f'repo = "org/name"\n{key} = {shown}\n')
                    loaded = config.load_repo_config("org/name", Path(temp), env={})
                self.assertEqual(getattr(loaded, key), value, f"{key} = {value!r}")
        defaults = RepoConfig()
        for pattern, key, expected in (
            (r"Epic mode \(on by default; `epic_mode = false`", "epic_mode", True),
            (r"`followup = true`, off by default", "followup", False),
            (r"\(`observed_issues`, on by default\)", "observed_issues", True),
            (r"`repo_skills` \(default true\)", "repo_skills", True),
            (r"`auto_build = false` or", "auto_build", True),  # off only when set to false
            (r"`comment_on_blocked = false` or", "comment_on_blocked", True),
        ):
            self.assertRegex(flat(text), pattern)
            self.assertIs(getattr(defaults, key), expected, key)

    def test_every_snake_case_word_it_names_is_a_known_term(self):
        text = read_skill()
        with tempfile.TemporaryDirectory() as temp:
            report = status_json(Path(temp) / "run1")
        known = (
            set(RepoConfig.__dataclass_fields__)
            | set(operations.JOB_STATUSES)
            | {f"waiting_{phase}" for phase in waiting_phases()}
            | set(reason_codes.ORDER)
            | set(preflight.CAPABILITIES)
            | set(report)
            | set(report["jobs"][0])
            | set(PLAN["jobs"][0])
            | set(schemas.VERIFY["properties"])
            | RECORD_KEYS
        )
        used = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", text))
        self.assertEqual(used - known, set(), "a snake_case word of the skill is not a config key, a status or a report key")


class StatusTests(unittest.TestCase):
    def test_the_keys_the_skill_lists_are_in_the_json_of_status(self):
        text = read_skill()
        job_keys = words(claim(r"`status --json` has `jobs\[\]` with (?P<v>.*?), and, at run level,", text))
        run_keys = words(claim(r"at run level, (?P<v>.*?)\. Before `run` has started", text))
        self.assertGreaterEqual(len(job_keys), 14)
        self.assertGreaterEqual(len(run_keys), 6)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "run1"
            report = status_json(directory)
            self.assertEqual(job_keys - set(report["jobs"][0]), set())
            self.assertEqual(run_keys - set(report), set())
            rows = {row["id"]: row for row in report["jobs"]}
            self.assertEqual(rows["fix-a"]["status"], "queued")
            self.assertIsNone(rows["fix-a"]["handoff_status"])  # no pull request yet: null
            self.assertEqual(rows["fix-b"]["handoff_status"], "published")  # --offline: every job with a PR
            # The run-level keys that have a meaning of their own: what they hold when nothing is wrong.
            shown = {key.strip() for key in claim(r"`live_refresh` is `\{(?P<v>[^}]*)\}`", text).split(",")}
            self.assertEqual(set(report["live_refresh"]), shown)
            self.assertTrue(report["live_refresh"]["ok"])
            self.assertIsNone(report["breaker"])  # "not null while the stop-the-world breaker is tripped"
            self.assertIsNone(report["capacity"])  # "null when none"
            # The sentences that explain the three keys name keys of the report.
            self.assertEqual(claim(r"`(?P<v>[a-z_]+)` is not null while the manager's stop-the-world", text), "breaker")
            self.assertEqual(claim(r"`(?P<v>[a-z_]+)` is the pod-wide worker budget \(null when none", text), "capacity")
            self.assertEqual(shown, set(report["live_refresh"]))
            (directory / "state.json").unlink()
            planned = run_status(directory, "--json")  # nothing started: only the counts
        self.assertEqual(planned.splitlines()[0], "Planned 2 issues in 2 jobs")
        self.assertIn("Before `run` has started anything it only prints the planned issue and job counts", flat(text))

    def test_the_breaker_key_holds_the_marker_of_a_tripped_breaker_and_is_null_otherwise(self):
        text = read_skill()
        reasons = claim(r"stop-the-world breaker is tripped \((?P<v>.*?): every session fails", text)
        self.assertEqual(reasons, "a spend limit, a refused key or a 429 storm")
        self.assertLessEqual({"spend_limit", "auth", "rate_limit_unknown"}, set(stopworld.TRIPPING))  # those three trip it
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(stopworld.read_from_env({"FACTORY_DATA": temp}))
            (Path(temp) / stopworld.MARKER).write_text(json.dumps({"code": "spend_limit", "at": "2026-01-01T00:00:00Z"}))
            tripped = stopworld.read_from_env({"FACTORY_DATA": temp})
        self.assertEqual(tripped, {"code": "spend_limit", "at": "2026-01-01T00:00:00Z"})
        self.assertIn("it never clears itself, a person fixes the cause and clears it", flat(text))

    def test_a_job_that_waits_for_a_worker_slot_shows_its_waiting_status(self):
        for phase in sorted(waiting_phases()):
            state = {
                "jobs": {
                    "fix-a": {
                        "status": f"waiting_{phase}",
                        "pending_phase": phase,
                        "waiting_for": f"{phase} capacity",
                        "waiting_owner": "factory",
                    },
                    "fix-b": STATE["jobs"]["fix-b"],
                }
            }
            with tempfile.TemporaryDirectory() as temp:
                report = status_json(Path(temp) / "run1", state)
            row = report["jobs"][0]
            self.assertEqual(row["status"], f"waiting_{phase}")
            self.assertTrue(row["waits"], phase)
            self.assertIn(f"{phase} capacity", " ".join(row["waits"]))


if __name__ == "__main__":
    unittest.main()
