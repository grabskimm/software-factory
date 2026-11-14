"""Per-repo config: strict TOML loading, base-branch rule and storage paths."""

import dataclasses
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from factory import config
from factory.config import (
    RepoConfig,
    RepoNotConfigured,
    check_base_branch,
    check_run_slug,
    data_paths,
    hidden_paths,
    load_repo_config,
    validate_base_branch,
)
from factory.phases import DEFAULT_PROBE_URL, AgentOptions, policy_for

REPO = "acme/widgets"

# Every key the file format documents. The sample below must contain all of them.
ALL_KEYS = {
    "repo",
    "base_branch",
    "allow_non_default_base",
    "installation_id",
    "toolchains",
    "setup_commands",
    "prompt_addendum",
    "verify.when",
    "acceptance.mode",
    "artifacts.roots",
    "sandbox.verify_domains",
    "preflight.probe_url",
    "model",
    "effort",
    "max_effort",
    "allowed_models",
    "models.plan",
    "models.build",
    "efforts.plan",
    "efforts.build",
    "aliases.fast",
    "budgets.plan",
    "budgets.build",
    "resources.max_active_agents",
    "worker.uid",
    "worker.gid",
    "abandon_after",
    "run_budget_usd",
    "planner_max_usd_per_hour",
    "auto_resume_after_restart",
    "auto_build",
    "repo_skills",
    "repo_skills_budget_kib",
    "auto_build_label",
    "auto_build_min_permission",
    "auto_build_concurrency",
    "auto_build_max_issues",
    "auto_build_timeout_seconds",
    "comment_on_blocked",
    "epic_mode",
    "epic_max_open_prs",
    "observed_issues",
    "pr_style",
    "execution",
    "followup",
    "followup_reply",
    "followup_min_permission",
    "followup_cooldown_seconds",
    "followup_max_sessions",
    "mention_handle",
}

SAMPLE = """\
repo = "acme/widgets"
base_branch = "release/1.2"
allow_non_default_base = true
installation_id = 4242
toolchains = ["node@22", "pnpm@9", "python@3.12"]
setup_commands = ["pnpm install --frozen-lockfile"]
prompt_addendum = '''
Prefer small commits.
Run `pnpm test` before finishing.
'''
model = "claude-sonnet-5-5"
effort = "medium"
max_effort = "xhigh"
allowed_models = ["claude-sonnet-5-5", "claude-opus-5-5"]
abandon_after = "3d"
run_budget_usd = 25
planner_max_usd_per_hour = 1.5
auto_resume_after_restart = "once"
auto_build = false
repo_skills = false
repo_skills_budget_kib = 600
auto_build_label = "ship it"
auto_build_min_permission = "maintain"
auto_build_concurrency = 2
auto_build_max_issues = 5
auto_build_timeout_seconds = 5400
comment_on_blocked = false
epic_mode = false
epic_max_open_prs = 2
observed_issues = false
pr_style = "conventional"
execution = "pod"
followup = true
followup_reply = false
followup_min_permission = "maintain"
followup_cooldown_seconds = 120
followup_max_sessions = 3
mention_handle = "example-bot"

[verify]
when = "always"

[acceptance]
mode = "recording"

[artifacts]
roots = ["plans/active", "docs/recordings"]

[sandbox]
verify_domains = ["registry.npmjs.org", "*.pythonhosted.org", "pypi.org:443"]

[preflight]
probe_url = "https://registry.npmjs.org/-/ping"

[models]
plan = "claude-opus-5-5"
build = "claude-sonnet-5-5"

[efforts]
plan = "high"
build = "low"

[aliases]
fast = "claude-haiku-5-5"

[budgets]
plan = 7
build = 9.5

[resources]
max_active_agents = 2

[worker]
uid = 10002
gid = 10002
"""


def flatten(document):
    keys = set()
    for key, value in document.items():
        if isinstance(value, dict):
            keys.update(f"{key}.{sub}" for sub in value)
        else:
            keys.add(key)
    return keys


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_dir = self.root / "config"

    def write(self, text, repo=REPO):
        path = self.config_dir / "repos" / f"{repo.replace('/', '__')}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def load(self, text, repo=REPO, **kwargs):
        self.write(text, repo)
        return load_repo_config(repo, self.config_dir, env={}, **kwargs)

    def body(self, extra=""):
        return f'repo = "{REPO}"\n{extra}'

    def assertRejected(self, text, fragment, **kwargs):
        with self.assertRaises(ValueError) as caught:
            self.load(text, **kwargs)
        self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)


class DefaultsAndRoundTrip(ConfigTestCase):
    def test_minimal_file_gets_every_default(self):
        cfg = self.load(self.body())
        self.assertEqual(
            cfg,
            RepoConfig(
                repo=REPO,
                base_branch="main",
                allow_non_default_base=False,
                installation_id=None,
                toolchains=(),
                setup_commands=(),
                verify_when="feature",
                acceptance_mode="tests",
                artifact_roots=(),
                verify_domains=(),
                probe_url=DEFAULT_PROBE_URL,
                prompt_addendum="",
                models={},
                budgets={},
                max_active_agents=6,
                worker_uid=None,
                worker_gid=None,
                abandon_after=604800,
                run_budget_usd=None,
                auto_resume_after_restart="never",
            ),
        )
        self.assertEqual((cfg.slug, cfg.owner, cfg.name), ("acme__widgets", "acme", "widgets"))
        self.assertEqual(cfg.abandon_after, 7 * 24 * 3600)

    def test_the_sample_exercises_every_documented_key(self):
        self.assertEqual(flatten(tomllib.loads(SAMPLE)), ALL_KEYS)

    def test_every_key_round_trips(self):
        cfg = self.load(SAMPLE)
        self.assertEqual(cfg.repo, REPO)
        self.assertEqual(cfg.base_branch, "release/1.2")
        self.assertIs(cfg.allow_non_default_base, True)
        self.assertEqual(cfg.installation_id, 4242)
        self.assertEqual(cfg.toolchains, ("node@22", "pnpm@9", "python@3.12"))
        self.assertEqual(cfg.setup_commands, ("pnpm install --frozen-lockfile",))
        self.assertEqual(cfg.prompt_addendum, "Prefer small commits.\nRun `pnpm test` before finishing.\n")
        self.assertEqual(cfg.verify_when, "always")
        self.assertEqual(cfg.acceptance_mode, "recording")
        self.assertEqual(cfg.artifact_roots, ("plans/active", "docs/recordings"))
        self.assertEqual(
            cfg.verify_domains, ("*.pythonhosted.org", "pypi.org:443", "registry.npmjs.org")
        )
        self.assertEqual(cfg.probe_url, "https://registry.npmjs.org/-/ping")
        self.assertEqual(cfg.models, {"plan": "claude-opus-5-5", "build": "claude-sonnet-5-5"})
        self.assertEqual(cfg.budgets, {"plan": 7.0, "build": 9.5})
        self.assertEqual((cfg.model, cfg.effort, cfg.max_effort), ("claude-sonnet-5-5", "medium", "xhigh"))
        self.assertEqual(cfg.allowed_models, ("claude-sonnet-5-5", "claude-opus-5-5"))
        self.assertEqual(cfg.efforts, {"plan": "high", "build": "low"})
        self.assertEqual(cfg.aliases, {"fast": "claude-haiku-5-5"})
        self.assertEqual(cfg.max_active_agents, 2)
        self.assertEqual((cfg.worker_uid, cfg.worker_gid), (10002, 10002))
        self.assertEqual(cfg.abandon_after, 3 * 24 * 3600)
        self.assertEqual(cfg.run_budget_usd, 25.0)
        self.assertIsInstance(cfg.run_budget_usd, float)
        self.assertEqual(cfg.planner_max_usd_per_hour, 1.5)
        self.assertEqual(cfg.auto_resume_after_restart, "once")
        self.assertEqual(
            (
                cfg.auto_build,
                cfg.auto_build_label,
                cfg.auto_build_min_permission,
                cfg.auto_build_concurrency,
                cfg.auto_build_max_issues,
                cfg.auto_build_timeout_seconds,
            ),
            (False, "ship it", "maintain", 2, 5, 5400),
        )
        self.assertIs(cfg.comment_on_blocked, False)
        self.assertIs(cfg.epic_mode, False)
        self.assertEqual(cfg.epic_max_open_prs, 2)
        self.assertIs(cfg.observed_issues, False)
        self.assertEqual(
            (cfg.followup, cfg.followup_min_permission, cfg.followup_cooldown_seconds, cfg.followup_max_sessions),
            (True, "maintain", 120, 3),
        )
        self.assertEqual(cfg.mention_handle, "example-bot")
        self.assertIs(cfg.followup_reply, False)

    def test_agent_options_maps_the_policy_inputs(self):
        cfg = self.load(SAMPLE)
        options = cfg.agent_options()
        self.assertIsInstance(options, AgentOptions)
        self.assertEqual(options.verify_domains, cfg.verify_domains)
        self.assertEqual(options.probe_url, "https://registry.npmjs.org/-/ping")
        self.assertEqual(options.models, {"plan": "claude-opus-5-5", "build": "claude-sonnet-5-5"})
        self.assertEqual(options.budgets, {"plan": 7.0, "build": 9.5})
        self.assertEqual(options.efforts, {"plan": "high", "build": "low"})
        self.assertEqual((options.default_model, options.default_effort), ("claude-sonnet-5-5", "medium"))
        self.assertEqual(options.aliases, {"fast": "claude-haiku-5-5"})
        self.assertEqual(options.allowed_models, ("claude-sonnet-5-5", "claude-opus-5-5"))
        self.assertEqual(options.max_effort, "xhigh")
        self.assertEqual(options.addendum, cfg.prompt_addendum)
        # The policy table accepts what config produced.
        self.assertEqual(policy_for("build-1", options).model, "claude-sonnet-5-5")
        self.assertEqual(policy_for("plan", options).budget_usd, 7.0)
        self.assertEqual(policy_for("review", options).budget_usd, 4.0)

    def test_agent_options_hands_out_copies(self):
        cfg = RepoConfig(REPO, models={"build": "m-1"}, budgets={"build": 1})
        options = cfg.agent_options()
        options.models["build"] = "tampered"
        options.budgets["build"] = 99.0
        self.assertEqual(cfg.models, {"build": "m-1"})
        self.assertEqual(cfg.budgets, {"build": 1.0})

    def test_default_agent_options_equal_the_policy_defaults(self):
        self.assertEqual(RepoConfig(REPO).agent_options(), AgentOptions())

    def test_config_is_immutable_and_detached_from_its_inputs(self):
        models, domains = {"build": "m-1"}, ["pypi.org"]
        cfg = RepoConfig(REPO, models=models, verify_domains=domains)
        for name, value in (("base_branch", "dev"), ("verify_when", "never"), ("repo", "a/b")):
            with self.subTest(name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(cfg, name, value)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            del cfg.repo
        models["build"] = "tampered"
        domains.append("evil.example")
        self.assertEqual(cfg.models, {"build": "m-1"})
        self.assertEqual(cfg.verify_domains, ("pypi.org",))
        self.assertIsInstance(cfg.verify_domains, tuple)

    def test_copies_made_with_replace_are_validated_again(self):
        cfg = RepoConfig(REPO, models={"build": "m-1"})
        copy = dataclasses.replace(cfg, base_branch="release/2")
        self.assertEqual((copy.base_branch, copy.models), ("release/2", {"build": "m-1"}))
        for change in ({"base_branch": "-x"}, {"repo": "../x/y"}, {"verify_when": "sometimes"}):
            with self.subTest(change), self.assertRaises(ValueError):
                dataclasses.replace(cfg, **change)

    def test_config_serializes_for_status_output(self):
        import json

        document = dataclasses.asdict(self.load(SAMPLE))
        self.assertEqual(json.loads(json.dumps(document))["verify_domains"][-1], "registry.npmjs.org")

    def test_the_documentation_example_is_a_valid_config_that_documents_every_top_level_key(self):
        reference = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"
        block = re.search(r"```toml\n(.*?)```", reference.read_text(encoding="utf-8"), re.S)
        self.assertIsNotNone(block, "docs/configuration.md lost its configuration example")
        document = tomllib.loads(block.group(1))
        # Strict loading: a typo or a stale key in the documentation fails here.
        RepoConfig(**config._fields_from_document(document))
        self.assertEqual({key for key in ALL_KEYS if "." not in key}, {key for key in document if not isinstance(document[key], dict)})

    def test_every_field_has_a_validator(self):
        self.assertEqual(
            {item.name for item in dataclasses.fields(RepoConfig)}, set(config._NORMALIZERS)
        )


class LocatingTheFile(ConfigTestCase):
    def test_config_dir_comes_from_the_environment(self):
        self.write(self.body())
        cfg = load_repo_config(REPO, env={"FACTORY_CONFIG": str(self.config_dir)})
        self.assertEqual(cfg.repo, REPO)

    def test_explicit_config_dir_beats_the_environment(self):
        self.write(self.body())
        cfg = load_repo_config(REPO, self.config_dir, env={"FACTORY_CONFIG": str(self.root / "nope")})
        self.assertEqual(cfg.repo, REPO)

    def test_no_config_dir_at_all(self):
        for env in ({}, {"FACTORY_CONFIG": ""}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, "FACTORY_CONFIG"):
                load_repo_config(REPO, env=env)

    def test_missing_file_means_the_repo_is_not_configured(self):
        self.write(self.body(), "acme/other")
        with self.assertRaises(RepoNotConfigured) as caught:
            load_repo_config(REPO, self.config_dir, env={})
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIn("acme__widgets.toml", str(caught.exception))

    def test_missing_config_dir_and_repos_that_are_not_directories(self):
        with self.assertRaises(RepoNotConfigured):
            load_repo_config(REPO, self.root / "absent", env={})
        self.config_dir.mkdir()
        (self.config_dir / "repos").write_text("a file, not a directory")
        with self.assertRaises(RepoNotConfigured):
            load_repo_config(REPO, self.config_dir, env={})

    def test_a_directory_in_place_of_the_file(self):
        (self.config_dir / "repos" / "acme__widgets.toml").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            load_repo_config(REPO, self.config_dir, env={})

    def test_symlinked_config_files_are_followed(self):
        # Mounted config volumes publish files through symlinks.
        target = self.root / "mounted.toml"
        target.write_text(self.body())
        link = self.config_dir / "repos" / "acme__widgets.toml"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.assertEqual(load_repo_config(REPO, self.config_dir, env={}).repo, REPO)

    def test_dotdot_in_the_config_dir_is_refused(self):
        self.write(self.body())
        with self.assertRaisesRegex(ValueError, r"\.\."):
            load_repo_config(REPO, self.config_dir / ".." / "config", env={})

    def test_requested_repo_cannot_reach_other_files(self):
        self.write(self.body())
        evil = [
            "../acme/widgets",
            "acme/../widgets",
            "acme/widgets/extra",
            "acme",
            "",
            "acme/",
            "/widgets",
            "acme/widgets\n",
            " acme/widgets",
            "acme/wid gets",
            "acme/.",
            "acme/..",
            "a__b/c",
            "acme_/widgets",
            "-acme/widgets",
            "acme/wid\x00gets",
            f"acme/{'x' * 300}",
            None,
            7,
        ]
        for repo in evil:
            with self.subTest(repo=repo), self.assertRaises(ValueError) as caught:
                load_repo_config(repo, self.config_dir, env={})
            self.assertNotIsInstance(caught.exception, RepoNotConfigured)

    def test_slugs_are_unique_per_repo(self):
        repos = ("acme/widgets", "acme/_widgets", "a_b/c", "a/b_c", "a/_b_c", "ab/c", "a.b/c", "acme/widgets_")
        slugs = {RepoConfig(repo).slug for repo in repos}
        self.assertEqual(len(slugs), len(repos))
        self.assertEqual(RepoConfig("acme/_widgets").slug, "acme___widgets")

    def test_repo_names_with_dots_are_accepted(self):
        for repo in ("acme/.github", "my-org/web.app", "o/a_b-c.d", "A1/B2"):
            with self.subTest(repo=repo):
                self.assertEqual(self.load(f'repo = "{repo}"\n', repo).repo, repo)


class FileShape(ConfigTestCase):
    def test_invalid_toml(self):
        self.assertRejected("repo = \n", "not valid TOML")
        self.assertRejected('repo = "a/b"\nrepo = "a/b"\n', "not valid TOML")

    def test_not_utf8(self):
        path = self.write("")
        path.write_bytes(b'repo = "acme/widgets"\n# \xff\xfe\n')
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            load_repo_config(REPO, self.config_dir, env={})

    def test_oversized_file(self):
        self.write(self.body(f"# {'x' * config.MAX_CONFIG_BYTES}\n"))
        with self.assertRaisesRegex(ValueError, "larger than"):
            load_repo_config(REPO, self.config_dir, env={})

    def test_deeply_nested_input_is_a_value_error_not_a_crash(self):
        self.assertRejected(f"x = {'[' * 100000}\n", "not valid TOML")

    def test_repo_is_required(self):
        self.assertRejected('base_branch = "main"\n', "missing required key 'repo'")

    def test_repo_must_equal_the_requested_repo(self):
        self.assertRejected('repo = "acme/other"\n', "does not match requested")
        # Case is not folded: the allowlist is the set of files the operator wrote.
        self.assertRejected('repo = "ACME/widgets"\n', "does not match requested")

    def test_a_copied_file_cannot_masquerade_as_another_repo(self):
        self.write(self.body(), "acme/gadgets")
        with self.assertRaisesRegex(ValueError, "does not match requested"):
            load_repo_config("acme/gadgets", self.config_dir, env={})

    def test_unknown_top_level_keys_are_errors(self):
        for key in ("base_branche", "verify_domains", "Base_Branch", "dev", "required_check_name"):
            with self.subTest(key):
                message = self.assertRejected(self.body(f'{key} = "x"\n'), "unknown config key")
                self.assertIn(key, message)

    def test_unknown_tables_are_errors(self):
        self.assertRejected(self.body("[verfiy]\nwhen = 'always'\n"), "unknown config key 'verfiy'")
        self.assertRejected(self.body("[extras]\nx = 1\n"), "unknown config key")

    def test_unknown_keys_inside_known_tables_are_errors(self):
        cases = {
            "verify": 'whem = "always"',
            "acceptance": 'modes = "tests"',
            "artifacts": 'root = ["a"]',
            "sandbox": 'domains = ["a.com"]',
            "preflight": 'url = "https://a.com/"',
            "resources": 'max_agents = 2',
            "worker": 'user = 1',
        }
        for table, line in cases.items():
            with self.subTest(table):
                self.assertRejected(self.body(f"[{table}]\n{line}\n"), f"unknown config key '{table}.")

    def test_tables_and_scalars_cannot_swap_places(self):
        for text, fragment in (
            ('verify = "always"', "verify must be a table"),
            ("[[worker]]\nuid = 1", "worker must be a table"),
            ('models = "claude"', "models must be a table"),
            ('budgets = 5', "budgets must be a table"),
            ("[base_branch]\nx = 1", "base_branch"),
        ):
            with self.subTest(text):
                self.assertRejected(self.body(text + "\n"), fragment)
        self.assertRejected("[repo]\nx = 1\n", "does not match requested")

    def test_errors_name_the_file(self):
        message = self.assertRejected(self.body('base_branch = "-x"'), "base_branch")
        self.assertIn("acme__widgets.toml", message)


class BaseBranch(ConfigTestCase):
    GOOD = ("main", "release/1.2", "feature_x", "dev", "v1.0.0", "user/topic-1", "a.b", "x+y", "a@b", "hidden/x")
    BAD = {
        "-x": "start with '-'",
        "--upload-pack=evil": "start with '-'",
        "a..b": "'..'",
        "a b": "space",
        "a\tb": "control",
        "a\nb": "control",
        "a\\b": "git-reserved",
        "x.lock": ".lock",
        "dir/x.lock/y": ".lock",
        "@{u}": "'@{'",
        "main@{1}": "'@{'",
        "main/": "end with '/'",
        "/main": "start or end with '/'",
        "a//b": "'//'",
        "main.": "end with '.'",
        ".hidden": "starting with '.'",
        "a/.b": "starting with '.'",
        "a~1": "git-reserved",
        "a^": "git-reserved",
        "a:b": "git-reserved",
        "a?": "git-reserved",
        "a*": "git-reserved",
        "a[0]": "git-reserved",
        "café": "non-ASCII",
        "main\x00": "control",
        "@": "not a branch name",
        "HEAD": "not a branch name",
        "refs/heads/main": "not a branch name",
        "": "non-empty",
        "x" * 300: "too long",
    }

    def test_accepted_names(self):
        for name in self.GOOD:
            with self.subTest(name):
                self.assertEqual(check_base_branch(name), name)
                self.assertEqual(self.load(self.body(f'base_branch = "{name}"\n')).base_branch, name)

    def test_rejected_names(self):
        for name, fragment in self.BAD.items():
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                check_base_branch(name)
            self.assertIn(fragment, str(caught.exception))

    def test_rejected_names_in_a_file(self):
        for name in ("-x", "a..b", "a b", "x.lock", "@{u}", "main/"):
            with self.subTest(name):
                self.assertRejected(self.body(f'base_branch = "{name}"\n'), "base_branch")

    def test_non_string_branches(self):
        for value in ("5", "true", "['main']", "{ a = 1 }"):
            with self.subTest(value):
                self.assertRejected(self.body(f"base_branch = {value}\n"), "base_branch")


class FieldValidation(ConfigTestCase):
    def test_every_invalid_value_is_rejected(self):
        cases = [
            # (toml line(s) after repo, message fragment)
            ('installation_id = 0', "installation_id"),
            ('installation_id = -3', "installation_id"),
            ('installation_id = "12"', "installation_id"),
            ('installation_id = 1.5', "installation_id"),
            ('installation_id = true', "installation_id"),
            ('allow_non_default_base = "yes"', "allow_non_default_base"),
            ('allow_non_default_base = 1', "allow_non_default_base"),
            ('toolchains = "node@22"', "toolchains must be a list"),
            ('toolchains = [""]', "toolchains[0]"),
            ('toolchains = ["  "]', "toolchains[0]"),
            ('toolchains = [22]', "toolchains[0]"),
            ('toolchains = ["node@22; rm -rf /"]', "toolchains[0]"),
            ('toolchains = ["$(id)"]', "toolchains[0]"),
            ('toolchains = ["a\\nb"]', "toolchains[0]"),
            ('setup_commands = "make"', "setup_commands must be a list"),
            ('setup_commands = [""]', "setup_commands[0]"),
            ('setup_commands = ["make", 3]', "setup_commands[1]"),
            ('setup_commands = ["make\\nrm -rf x"]', "setup_commands[0]"),
            ('setup_commands = ["make\\rx"]', "setup_commands[0]"),
            ('setup_commands = ["make\\u0000x"]', "setup_commands[0]"),
            ('setup_commands = ["make\\u001b[2Jx"]', "setup_commands[0]"),
            ('prompt_addendum = 5', "prompt_addendum"),
            ('prompt_addendum = ["a"]', "prompt_addendum"),
            ('prompt_addendum = "a\\u0000b"', "prompt_addendum"),
            (f'prompt_addendum = "{"x" * (config.MAX_ADDENDUM_CHARS + 1)}"', "prompt_addendum"),
            ('[verify]\nwhen = "sometimes"', "verify.when"),
            ('[verify]\nwhen = "Always"', "verify.when"),
            ('[verify]\nwhen = true', "verify.when"),
            ('[acceptance]\nmode = "video"', "acceptance.mode"),
            ('[acceptance]\nmode = 1', "acceptance.mode"),
            ('[artifacts]\nroots = "plans"', "artifacts.roots must be a list"),
            ('[artifacts]\nroots = [""]', "artifacts.roots[0]"),
            ('[artifacts]\nroots = ["/etc"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["../x"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a/../b"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a/*"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a\\\\b"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a//b"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["./a"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a/"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["."]', "artifacts.roots"),
            ('[artifacts]\nroots = [".git/hooks"]', "artifacts.roots"),
            ('[artifacts]\nroots = ["a/.git"]', "artifacts.roots"),
            ('[artifacts]\nroots = [" plans"]', "artifacts.roots"),
            ('[artifacts]\nroots = [5]', "artifacts.roots[0]"),
            ('[sandbox]\nverify_domains = "pypi.org"', "sandbox.verify_domains must be a list"),
            ('[sandbox]\nverify_domains = [1]', "sandbox.verify_domains[0]"),
            ('[sandbox]\nverify_domains = ["https://pypi.org"]', "invalid sandbox domain"),
            ('[sandbox]\nverify_domains = ["pypi.org/simple"]', "invalid sandbox domain"),
            ('[sandbox]\nverify_domains = ["*"]', "invalid sandbox domain"),
            ('[sandbox]\nverify_domains = ["a b.com"]', "invalid sandbox domain"),
            ('[sandbox]\nverify_domains = ["user@pypi.org"]', "invalid sandbox domain"),
            ('[sandbox]\nverify_domains = ["*.com"]', "registrable domain"),
            ('[sandbox]\nverify_domains = ["*.io:443"]', "registrable domain"),
            ('[preflight]\nprobe_url = "http://registry.npmjs.org/"', "https"),
            ('[preflight]\nprobe_url = "registry.npmjs.org"', "https"),
            ('[preflight]\nprobe_url = "https://"', "https"),
            ('[preflight]\nprobe_url = "file:///etc/passwd"', "https"),
            ('[preflight]\nprobe_url = "https://user:FAKE-pw@registry.npmjs.org/"', "credentials"),
            ('[preflight]\nprobe_url = "https://registry.npmjs.org:abc/"', "valid https URL"),
            ('[preflight]\nprobe_url = "https://registry.npmjs.org/ x"', "whitespace"),
            ('[preflight]\nprobe_url = " https://registry.npmjs.org/"', "whitespace"),
            ('[preflight]\nprobe_url = 5', "probe_url"),
            ('[models]\nbuild = ""', "models.build"),
            ('[models]\nbuild = 5', "models.build"),
            ('[models]\nbuild = "--model x"', "models.build"),
            ('[models]\nbuild = "-x"', "models.build"),
            ('[models]\nbuild = "a b"', "models.build"),
            ('[models]\nbuild = "a\\nb"', "models.build"),
            ('[models]\nbuilder = "m"', "unknown phase 'builder'"),
            ('[models]\n"build-0" = "m"', "unknown phase 'build-0'"),
            ('[budgets]\nbuild = 0', "budgets.build"),
            ('[budgets]\nbuild = -1', "budgets.build"),
            ('[budgets]\nbuild = "5"', "budgets.build"),
            ('[budgets]\nbuild = true', "budgets.build"),
            ('[budgets]\nbuild = inf', "budgets.build"),
            ('[budgets]\nbuild = nan', "budgets.build"),
            (f"[budgets]\nbuild = 1{'0' * 400}", "budgets.build"),
            ('[budgets]\nbuilder = 1', "unknown phase 'builder'"),
            ('[resources]\nmax_active_agents = 0', "max_active_agents"),
            ('[resources]\nmax_active_agents = -2', "max_active_agents"),
            ('[resources]\nmax_active_agents = 2.5', "max_active_agents"),
            ('[resources]\nmax_active_agents = "4"', "max_active_agents"),
            ('[resources]\nmax_active_agents = true', "max_active_agents"),
            ('[worker]\nuid = 10002', "set together"),
            ('[worker]\ngid = 10002', "set together"),
            ('[worker]\nuid = -1\ngid = 5', "worker.uid"),
            ('[worker]\nuid = 5\ngid = -1', "worker.gid"),
            ('[worker]\nuid = 0\ngid = 10002', "worker.uid"),
            ('[worker]\nuid = 10002\ngid = 0', "worker.gid"),
            ('[worker]\nuid = 4294967295\ngid = 5', "worker.uid"),
            ('[worker]\nuid = "10002"\ngid = 5', "worker.uid"),
            ('[worker]\nuid = 1.5\ngid = 5', "worker.uid"),
            ('[worker]\nuid = true\ngid = 5', "worker.uid"),
        ]
        for text, fragment in cases:
            with self.subTest(text[:70]):
                self.assertRejected(self.body(text + "\n"), fragment)

    def test_artifact_roots_are_normalized_and_deduplicated(self):
        cfg = self.load(self.body('[artifacts]\nroots = ["plans/active", "docs", "plans/active"]\n'))
        self.assertEqual(cfg.artifact_roots, ("plans/active", "docs"))

    def test_boundary_values_are_accepted(self):
        cfg = self.load(
            self.body(
                "installation_id = 1\n[resources]\nmax_active_agents = 1\n"
                "[worker]\nuid = 1\ngid = 4294967294\n[budgets]\npreflight = 0.01\nverify = 3\n"
            )
        )
        self.assertEqual((cfg.installation_id, cfg.max_active_agents), (1, 1))
        self.assertEqual((cfg.worker_uid, cfg.worker_gid), (1, 4294967294))
        self.assertEqual(cfg.budgets, {"preflight": 0.01, "verify": 3.0})
        self.assertIsInstance(cfg.budgets["verify"], float)

    def test_every_phase_name_is_accepted_in_models_and_budgets(self):
        from factory.phases import PHASES

        models = "\n".join(f'{p} = "claude-x-{p}"' for p in PHASES)
        budgets = "\n".join(f"{p} = 2" for p in PHASES)
        cfg = self.load(self.body(f"[models]\n{models}\n[budgets]\n{budgets}\n"))
        self.assertEqual(set(cfg.models), set(PHASES))
        self.assertEqual(set(cfg.budgets), set(PHASES))

    def test_model_ids_with_provider_suffixes_are_accepted(self):
        for model in ("claude-sonnet-5-5[1m]", "sonnet", "claude-x@20260101", "us.claude-x-v1:0"):
            with self.subTest(model):
                self.assertEqual(RepoConfig(REPO, models={"build": model}).models["build"], model)

    def test_programmatic_construction_is_validated_too(self):
        for kwargs in (
            {"repo": "../x/y"},
            {"base_branch": "-x"},
            {"verify_when": "sometimes"},
            {"verify_domains": "pypi.org"},
            {"toolchains": "node"},
            {"models": {"nope": "m"}},
            {"budgets": {"build": float("inf")}},
            {"worker_uid": 10002},
        ):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                RepoConfig(**{"repo": REPO, **kwargs})

    def test_error_messages_do_not_echo_private_text(self):
        secret = "FAKE-private-marker-123"
        cases = [
            f'setup_commands = ["curl -H \\"{secret}\\"\\nx"]',
            f'prompt_addendum = {{ x = "{secret}" }}',
            f'[preflight]\nprobe_url = "https://u:{secret}@registry.npmjs.org/"',
            f'[preflight]\nprobe_url = "https://registry.npmjs.org/?t={secret} x"',
            f'[preflight]\nprobe_url = "http://registry.npmjs.org/?t={secret}"',
        ]
        for text in cases:
            with self.subTest(text[:50]), self.assertRaises(ValueError) as caught:
                self.load(self.body(text + "\n"))
            self.assertNotIn(secret, str(caught.exception))

    def test_a_long_bad_value_is_truncated_in_the_message(self):
        message = self.assertRejected(self.body(f'[verify]\nwhen = "{"y" * 5000}"\n'), "verify.when")
        self.assertLess(len(message), 400)


class ModelChoiceKeys(ConfigTestCase):
    """model, effort, [efforts], allowed_models, max_effort and [aliases] (src/factory/model_choice.py)."""

    def test_a_config_with_only_models_keeps_every_new_field_unset(self):
        cfg = self.load(self.body('[models]\nbuild = "claude-sonnet-5-5"\n'))
        self.assertEqual(
            (cfg.model, cfg.effort, cfg.efforts, cfg.allowed_models, cfg.max_effort, cfg.aliases),
            (None, None, {}, None, None, {}),
        )
        self.assertEqual(cfg.agent_options(), AgentOptions(models={"build": "claude-sonnet-5-5"}))
        for phase in ("plan", "build", "review", "verify", "preflight"):
            self.assertEqual(policy_for(phase, cfg.agent_options()).effort, policy_for(phase).effort, phase)

    def test_rejected_values(self):
        for text, fragment in (
            ('model = ""', "model must be a model id"),
            ('model = 5', "model must be a model id"),
            ('model = "-x"', "model must be a model id"),
            ('model = "a b"', "model must be a model id"),
            ('effort = "extreme"', "effort must be one of"),
            ('effort = 3', "effort must be one of"),
            ('max_effort = "ultra"', "max_effort must be one of"),
            ('allowed_models = "claude-x"', "allowed_models must be a list"),
            ('allowed_models = ["ok", 5]', "allowed_models[1]"),
            ('allowed_models = ["a b"]', "allowed_models[0]"),
            ('[efforts]\nbuild = "extreme"', "efforts.build"),
            ('[efforts]\nbuild = 5', "efforts.build"),
            ('[efforts]\nbuilder = "low"', "unknown phase 'builder'"),
            ('efforts = "low"', "efforts must be a table"),
            ('aliases = "opus"', "aliases must be a table"),
            ('[aliases]\nOpus = "claude-opus-5-5"', "aliases: 'Opus'"),
            ('[aliases]\n"a b" = "claude-opus-5-5"', "aliases: 'a b'"),
            ('[aliases]\nfast = "a b"', "aliases.fast"),
            ('[aliases]\nfast = 5', "aliases.fast"),
            ('[aliases]\nfast = ""', "aliases.fast"),
            (f'[aliases]\n{"a" * 37} = "m"', "aliases:"),
        ):
            with self.subTest(text):
                self.assertRejected(self.body(f"{text}\n"), fragment)

    def test_too_many_aliases_or_allowed_models_are_rejected(self):
        many = "\n".join(f'a{n} = "claude-x-{n}"' for n in range(config.MAX_ALIASES + 1))
        self.assertRejected(self.body(f"[aliases]\n{many}\n"), "aliases must be a table")
        listed = ", ".join(f'"claude-x-{n}"' for n in range(config.MAX_ALLOWED_MODELS + 1))
        self.assertRejected(self.body(f"allowed_models = [{listed}]\n"), "allowed_models must be a list")

    def test_valid_values_are_normalised(self):
        cfg = self.load(
            self.body(
                'model = "claude-opus-5-5"\neffort = "max"\nmax_effort = "low"\n'
                'allowed_models = ["m-1", "m-2", "m-1"]\n[aliases]\nfast = "m-3"\n"x.y_z-9" = "m-4"\n'
            )
        )
        # A default effort above max_effort is fine: the cap only bounds what an issue may select.
        self.assertEqual((cfg.effort, cfg.max_effort), ("max", "low"))
        self.assertEqual(cfg.allowed_models, ("m-1", "m-2"))
        self.assertEqual(cfg.aliases, {"fast": "m-3", "x.y_z-9": "m-4"})
        self.assertEqual(self.load(self.body("allowed_models = []\n")).allowed_models, ())

    def test_the_keys_are_top_level_or_their_own_table_only(self):
        for text in ('[models]\nmodel = "m"', '[efforts]\neffort = "low"', '[aliases]\nmodel = "m"\n[verify]\nmodel = "m"'):
            with self.subTest(text), self.assertRaises(ValueError):
                self.load(self.body(f"{text}\n"))

    def test_programmatic_construction_is_validated_too(self):
        for kwargs in (
            {"model": "a b"},
            {"effort": "extreme"},
            {"max_effort": "extreme"},
            {"efforts": {"nope": "low"}},
            {"allowed_models": "m"},
            {"aliases": {"Bad": "m"}},
        ):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                RepoConfig(**{"repo": REPO, **kwargs})


class ManagerPolicyKeys(ConfigTestCase):
    """abandon_after, run_budget_usd and auto_resume_after_restart: read by the manager."""

    def test_abandon_after_units_become_whole_seconds(self):
        for text, seconds in (
            ('"7d"', 604800),
            ('"1d"', 86400),
            ('"36h"', 129600),
            ('"90m"', 5400),
            ('"60m"', 3600),
            ('"1h"', 3600),
            ('"3650d"', 3650 * 86400),
            ("3600", 3600),
            ("86400", 86400),
        ):
            with self.subTest(text):
                cfg = self.load(self.body(f"abandon_after = {text}\n"))
                self.assertEqual(cfg.abandon_after, seconds)
                self.assertIs(type(cfg.abandon_after), int)

    def test_abandon_after_rejects_everything_else(self):
        cases = [
            '"7"',  # no unit: days, hours or seconds?
            '"7D"',
            '"7 d"',
            '" 7d"',
            '"7d "',
            '"7d\\n"',
            '"d"',
            '""',
            '"7w"',
            '"7s"',
            '"-1d"',
            '"+1d"',
            '"1.5d"',
            '"0d"',
            '"0h"',
            '"59m"',  # under the one hour floor
            '"٧d"',  # a non-ASCII digit
            '"3651d"',
            "3599",  # seconds under the floor
            "0",
            "-86400",
            "7.0",
            "86400.0",
            "true",
            "[7]",
            "{ d = 7 }",
        ]
        for text in cases:
            with self.subTest(text):
                self.assertRejected(self.body(f"abandon_after = {text}\n"), "abandon_after")

    def test_abandon_after_bounds_hold_for_numbers_that_are_far_out_of_range(self):
        for value in (10**30, -(10**30), f"{'9' * 5000}d", f"{'9' * 9}d"):
            with self.subTest(str(value)[:20]), self.assertRaises(ValueError) as caught:
                RepoConfig(REPO, abandon_after=value)
            self.assertIn("abandon_after", str(caught.exception))
            self.assertLess(len(str(caught.exception)), 300)

    def test_abandon_after_survives_dataclasses_replace(self):
        # The stored seconds are validated again by replace(), so they must pass as a number.
        for text in ('"1h"', '"90m"', '"7d"', "7200"):
            with self.subTest(text):
                cfg = self.load(self.body(f"abandon_after = {text}\n"))
                self.assertEqual(dataclasses.replace(cfg, base_branch="main").abandon_after, cfg.abandon_after)

    def test_run_budget_is_a_positive_finite_float(self):
        for text, expected in (("25", 25.0), ("0.5", 0.5), ("12.75", 12.75), ("1e3", 1000.0), ("0.0001", 0.0001)):
            with self.subTest(text):
                cfg = self.load(self.body(f"run_budget_usd = {text}\n"))
                self.assertEqual(cfg.run_budget_usd, expected)
                self.assertIsInstance(cfg.run_budget_usd, float)

    def test_run_budget_rejects_everything_else(self):
        for text in ("0", "0.0", "-1", "-0.5", "nan", "inf", "-inf", '"25"', "true", "false", "[25]", f"1{'0' * 400}"):
            with self.subTest(text):
                self.assertRejected(self.body(f"run_budget_usd = {text}\n"), "run_budget_usd")

    def test_run_budget_that_overflows_a_float_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "run_budget_usd"):
            RepoConfig(REPO, run_budget_usd=10**400)

    def test_run_budget_none_means_no_guardrail_and_stays_none(self):
        self.assertIsNone(RepoConfig(REPO).run_budget_usd)
        self.assertIsNone(RepoConfig(REPO, run_budget_usd=None).run_budget_usd)

    def test_auto_resume_accepts_only_never_and_once(self):
        for mode in ("never", "once"):
            with self.subTest(mode):
                cfg = self.load(self.body(f'auto_resume_after_restart = "{mode}"\n'))
                self.assertEqual(cfg.auto_resume_after_restart, mode)
        for text in ('"always"', '"Once"', '"ONCE"', '" once"', '""', "true", "1", "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"auto_resume_after_restart = {text}\n"), "auto_resume_after_restart")
        with self.assertRaises(ValueError):
            RepoConfig(REPO, auto_resume_after_restart=None)

    def test_the_session_timeout_is_60_to_86400_and_anything_else_is_the_default(self):
        for value in (60, 1800, 5400, 86400):
            with self.subTest(value=value):
                self.assertEqual(self.load(self.body(f"auto_build_timeout_seconds = {value}\n")).auto_build_timeout_seconds, value)
        for text in ("0", "59", "86401", "-1", "1800.0", "true", '"3600"', "[]"):
            with self.subTest(text=text):
                messages = []
                cfg = self.load(self.body(f"auto_build_timeout_seconds = {text}\n"), log=messages.append)
                self.assertEqual(cfg.auto_build_timeout_seconds, 1800)
                self.assertEqual(len(messages), 1)
                self.assertIn("auto_build_timeout_seconds", messages[0])
        self.assertEqual(RepoConfig(REPO, auto_build_timeout_seconds=10).auto_build_timeout_seconds, 1800)

    def test_the_keys_belong_at_the_top_level_only(self):
        for table in ("resources", "verify", "worker"):
            for key, value in (("abandon_after", '"7d"'), ("run_budget_usd", "5"), ("auto_resume_after_restart", '"once"')):
                with self.subTest(f"{table}.{key}"):
                    self.assertRejected(self.body(f"[{table}]\n{key} = {value}\n"), f"unknown config key '{table}.{key}'")

    def test_long_bad_values_are_cut_in_the_message(self):
        message = self.assertRejected(self.body(f'abandon_after = "{"z" * 5000}"\n'), "abandon_after")
        self.assertLess(len(message), 400)
        message = self.assertRejected(self.body(f'auto_resume_after_restart = "{"z" * 5000}"\n'), "auto_resume_after_restart")
        self.assertLess(len(message), 400)


class PlannerHourlyCapKey(ConfigTestCase):
    """planner_max_usd_per_hour: the planner spend the trigger loop allows per repository per hour (src/factory/operations.py)."""

    def test_the_default_is_two_dollars(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertEqual(cfg.planner_max_usd_per_hour, 2.0)
            self.assertIsInstance(cfg.planner_max_usd_per_hour, float)

    def test_a_finite_number_of_dollars_is_taken_as_a_float(self):
        for text, expected in (("5", 5.0), ("0.25", 0.25), ("1e2", 100.0)):
            with self.subTest(text):
                cfg = self.load(self.body(f"planner_max_usd_per_hour = {text}\n"))
                self.assertEqual(cfg.planner_max_usd_per_hour, expected)
                self.assertIsInstance(cfg.planner_max_usd_per_hour, float)

    def test_zero_and_off_disable_the_cap_and_survive_replace(self):
        for text in ("0", "0.0", '"off"', '"OFF"'):
            with self.subTest(text):
                cfg = self.load(self.body(f"planner_max_usd_per_hour = {text}\n"))
                self.assertEqual(cfg.planner_max_usd_per_hour, 0.0)
                self.assertEqual(dataclasses.replace(cfg, base_branch="main").planner_max_usd_per_hour, 0.0)

    def test_negative_non_finite_and_non_numeric_values_are_refused(self):
        for text in ("-1", "-0.5", "nan", "inf", "-inf", '"2"', '"on"', '""', "true", "false", "[2]", f"1{'0' * 400}"):
            with self.subTest(text):
                self.assertRejected(self.body(f"planner_max_usd_per_hour = {text}\n"), "planner_max_usd_per_hour")
        self.assertRejected(self.body("[verify]\nplanner_max_usd_per_hour = 2\n"), "unknown config key")
        with self.assertRaisesRegex(ValueError, "planner_max_usd_per_hour"):
            RepoConfig(REPO, planner_max_usd_per_hour=10**400)


class CommentOnBlockedKey(ConfigTestCase):
    """comment_on_blocked: the per-repository switch of the outcome comment (src/factory/issue_comments.py)."""

    def test_defaults_to_commenting(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertIs(cfg.comment_on_blocked, True)

    def test_is_a_real_boolean_at_the_top_level(self):
        self.assertIs(self.load(self.body("comment_on_blocked = false\n")).comment_on_blocked, False)
        self.assertIs(self.load(self.body("comment_on_blocked = true\n")).comment_on_blocked, True)
        for text in ('"false"', "0", "1", '"yes"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"comment_on_blocked = {text}\n"), "comment_on_blocked")
        self.assertRejected(self.body("[verify]\ncomment_on_blocked = false\n"), "unknown config key")


class EpicModeKey(ConfigTestCase):
    """epic_mode: on by default for every repository (src/factory/epic.py); ``false`` keeps today's ``blocked``."""

    def test_defaults_to_on(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertIs(cfg.epic_mode, True)

    def test_is_a_real_boolean_at_the_top_level(self):
        self.assertIs(self.load(self.body("epic_mode = false\n")).epic_mode, False)
        self.assertIs(self.load(self.body("epic_mode = true\n")).epic_mode, True)
        for text in ('"false"', "0", "1", '"yes"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"epic_mode = {text}\n"), "epic_mode")
        self.assertRejected(self.body("[verify]\nepic_mode = false\n"), "unknown config key")


class EpicMaxOpenPrsKey(ConfigTestCase):
    """epic_max_open_prs: the open pull request cap of the automatic start of an epic phase (src/factory/epic_gate.py)."""

    def test_defaults_to_three_and_takes_a_whole_number_from_one_to_fifty(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertEqual(cfg.epic_max_open_prs, 3)
        for value in (1, 3, 50):
            self.assertEqual(self.load(self.body(f"epic_max_open_prs = {value}\n")).epic_max_open_prs, value)

    def test_anything_else_is_refused_and_it_is_a_top_level_key_only(self):
        for text in ("0", "-1", "51", "true", "2.5", '"3"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"epic_max_open_prs = {text}\n"), "epic_max_open_prs")
        self.assertRejected(self.body("[verify]\nepic_max_open_prs = 2\n"), "unknown config key")


class ObservedIssuesKey(ConfigTestCase):
    """observed_issues: the per-repository switch of the issues opened for observations (src/factory/observed_issues.py)."""

    def test_defaults_to_on(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertIs(cfg.observed_issues, True)

    def test_is_a_real_boolean_at_the_top_level(self):
        self.assertIs(self.load(self.body("observed_issues = false\n")).observed_issues, False)
        self.assertIs(self.load(self.body("observed_issues = true\n")).observed_issues, True)
        for text in ('"false"', "0", "1", '"yes"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"observed_issues = {text}\n"), "observed_issues")
        self.assertRejected(self.body("[verify]\nobserved_issues = false\n"), "unknown config key")


class PrStyleKey(ConfigTestCase):
    """pr_style: how a pull request is titled and its commit worded (src/factory/conventions.py)."""

    def test_defaults_to_the_factory_style(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertEqual(cfg.pr_style, "factory")

    def test_is_one_of_the_known_styles(self):
        self.assertEqual(self.load(self.body('pr_style = "conventional"\n')).pr_style, "conventional")
        self.assertEqual(self.load(self.body('pr_style = "factory"\n')).pr_style, "factory")
        for text in ('"semantic"', '"Conventional"', "true", "1", "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"pr_style = {text}\n"), "pr_style")
        self.assertRejected(self.body('[verify]\npr_style = "conventional"\n'), "unknown config key")


class FollowupKeys(ConfigTestCase):
    """followup and friends: read by the manager's follow-up pass (docs/manager.md, "Follow-up requests")."""

    def test_follow_ups_are_off_by_default_with_the_safe_limits(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertEqual(
                (cfg.followup, cfg.followup_min_permission, cfg.followup_cooldown_seconds, cfg.followup_max_sessions),
                (False, "write", 300, 5),
            )
            self.assertEqual(cfg.mention_handle, "")

    def test_the_reply_on_the_pull_request_is_on_by_default_and_a_real_boolean(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertIs(cfg.followup_reply, True)  # it only matters where ``followup`` is on
        self.assertIs(self.load(self.body("followup_reply = false\n")).followup_reply, False)
        self.assertIs(self.load(self.body("followup_reply = true\n")).followup_reply, True)
        for text in ('"false"', "0", "1", '"no"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"followup_reply = {text}\n"), "followup_reply must be true or false")
        self.assertRejected(self.body("[verify]\nfollowup_reply = false\n"), "unknown config key")

    def test_mention_handle_is_an_account_name(self):
        self.assertEqual(self.load(self.body('mention_handle = "Factory-Bot-1"\n')).mention_handle, "Factory-Bot-1")
        for text in ('"a b"', '"-x"', '"x_y"', '"@x"', '"a/b"', '"x[bot]"', f'"{"a" * 40}"', "1", "true", "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"mention_handle = {text}\n"), "mention_handle")

    def test_followup_is_a_real_boolean(self):
        self.assertIs(self.load(self.body("followup = true\n")).followup, True)
        for text in ('"false"', "0", "1", '"no"', "[]"):
            with self.subTest(text):
                self.assertRejected(self.body(f"followup = {text}\n"), "followup must be true or false")
        with self.assertRaises(ValueError):
            RepoConfig(REPO, followup=None)

    def test_the_minimum_permission_is_one_of_the_levels_like_auto_build(self):
        for level in config.PERMISSION_LEVELS:  # below write is refused later, for a repository that is not private
            with self.subTest(level):
                self.assertEqual(self.load(self.body(f'followup_min_permission = "{level}"\n')).followup_min_permission, level)
        for text in ('"owner"', '"Write"', '"none"', '""', "1", "true"):
            with self.subTest(text):
                self.assertRejected(self.body(f"followup_min_permission = {text}\n"), "followup_min_permission")

    def test_cooldown_and_session_cap_are_bounded_integers(self):
        for key, low, high in (("followup_cooldown_seconds", 0, 86400), ("followup_max_sessions", 1, 100)):
            for value in (low, high):
                with self.subTest(key, value=value):
                    self.assertEqual(getattr(self.load(self.body(f"{key} = {value}\n")), key), value)
            for text in (str(low - 1), str(high + 1), "1.5", "true", '"5"', "[]"):
                with self.subTest(key, text=text):
                    self.assertRejected(self.body(f"{key} = {text}\n"), key)

    def test_the_keys_belong_at_the_top_level(self):
        self.assertRejected(self.body("[verify]\nfollowup = false\n"), "unknown config key")


class LabelTriggerKeys(ConfigTestCase):
    """auto_build and friends: read by the manager's trigger loop (docs/manager.md, "Label-triggered builds")."""

    def test_defaults_watch_the_repository_with_the_safe_limits(self):
        for cfg in (RepoConfig(REPO), self.load(self.body())):
            self.assertEqual(
                (
                    cfg.auto_build,
                    cfg.auto_build_label,
                    cfg.auto_build_min_permission,
                    cfg.auto_build_concurrency,
                    cfg.auto_build_max_issues,
                    cfg.auto_build_timeout_seconds,
                ),
                (True, "factory-build", "write", 1, 3, 1800),
            )

    def test_auto_build_is_a_real_boolean(self):
        self.assertIs(self.load(self.body("repo_skills = false\n")).repo_skills, False)
        self.assertIs(self.load(self.body("")).repo_skills, True)
        for text in ('"yes"', "1", "0"):
            with self.subTest(repo_skills=text):
                self.assertRejected(self.body(f"repo_skills = {text}\n"), "")
        self.assertIs(self.load(self.body("auto_build = false\n")).auto_build, False)
        self.assertIs(self.load(self.body("auto_build = true\n")).auto_build, True)
        for text in ('"false"', "0", "1", '"no"', "[]", "off"):
            with self.subTest(text):
                self.assertRejected(self.body(f"auto_build = {text}\n"), "")
        self.assertRejected(self.body('auto_build = "false"\n'), "auto_build must be true or false")
        with self.assertRaises(ValueError):
            RepoConfig(REPO, auto_build=None)

    def test_the_label_is_a_plain_name_and_never_a_control_label(self):
        for label in ("factory-build", "build", "ship it", "team/build.1", "A" * 50, "x"):
            with self.subTest(label):
                self.assertEqual(self.load(self.body(f'auto_build_label = "{label}"\n')).auto_build_label, label)
        for label in (
            "", " x", "x ", "-x", "A" * 51, "a,b", "a:b", "a\\\"b", "a\\nb", "ü", "factory-ignore", "Factory-Ignore",
            "factory:planned", "Factory:Blocked", "factory:", "a'b", "a$b", "a{b",
        ):
            with self.subTest(label):
                self.assertRejected(self.body(f'auto_build_label = "{label}"\n'), "auto_build_label")
        for text in ("1", "true", "[]", "{}"):
            self.assertRejected(self.body(f"auto_build_label = {text}\n"), "auto_build_label")

    def test_the_minimum_permission_is_one_of_the_levels(self):
        for level in ("read", "triage", "write", "maintain", "admin"):
            with self.subTest(level):
                self.assertEqual(self.load(self.body(f'auto_build_min_permission = "{level}"\n')).auto_build_min_permission, level)
        for text in ('"owner"', '"Write"', '"none"', '""', "1", "true", '" write"'):
            with self.subTest(text):
                self.assertRejected(self.body(f"auto_build_min_permission = {text}\n"), "auto_build_min_permission")

    def test_the_counts_are_bounded_integers(self):
        for key, high in (("auto_build_concurrency", 64), ("auto_build_max_issues", 50)):
            for value in (1, high):
                with self.subTest(key=key, value=value):
                    self.assertEqual(getattr(self.load(self.body(f"{key} = {value}\n")), key), value)
            for text in ("0", "-1", str(high + 1), "1.0", "true", '"2"', "[]"):
                with self.subTest(key=key, text=text):
                    self.assertRejected(self.body(f"{key} = {text}\n"), key)

    def test_the_keys_belong_at_the_top_level_only(self):
        for table in ("resources", "verify", "worker"):
            with self.subTest(table):
                self.assertRejected(self.body(f"[{table}]\nauto_build = false\n"), f"unknown config key '{table}.auto_build'")

    def test_a_typo_is_refused_rather_than_leaving_the_default(self):
        self.assertRejected(self.body("auto_builds = false\n"), "unknown config key 'auto_builds'")
        self.assertRejected(self.body("auto_build_permission = \"read\"\n"), "unknown config key")


class ProbeUrlAgainstAllowlist(ConfigTestCase):
    def test_both_set_explicitly_and_inconsistent_is_an_error(self):
        text = self.body(
            '[sandbox]\nverify_domains = ["pypi.org"]\n[preflight]\nprobe_url = "https://registry.npmjs.org/"\n'
        )
        self.assertRejected(text, "not on sandbox.verify_domains")

    def test_consistent_pairs_pass(self):
        for domains, url in (
            ('["registry.npmjs.org"]', "https://registry.npmjs.org/"),
            ('["*.npmjs.org"]', "https://registry.npmjs.org/-/ping"),
            ('["pypi.org:443"]', "https://pypi.org/simple/"),
            ('["PyPI.org"]', "https://PYPI.org/"),
        ):
            with self.subTest(domains, url=url):
                warnings = []
                cfg = self.load(
                    self.body(
                        f'[sandbox]\nverify_domains = {domains}\n[preflight]\nprobe_url = "{url}"\n'
                    ),
                    log=warnings.append,
                )
                self.assertEqual(warnings, [])
                self.assertIsNone(cfg.probe_problem())

    def test_wildcard_does_not_cover_its_apex(self):
        text = self.body(
            '[sandbox]\nverify_domains = ["*.npmjs.org"]\n[preflight]\nprobe_url = "https://npmjs.org/"\n'
        )
        self.assertRejected(text, "not on sandbox.verify_domains")

    def test_default_probe_with_custom_domains_only_warns(self):
        warnings = []
        cfg = self.load(
            self.body('[sandbox]\nverify_domains = ["pypi.org"]\n'), log=warnings.append
        )
        self.assertEqual(cfg.probe_url, DEFAULT_PROBE_URL)
        self.assertEqual(len(warnings), 1)
        self.assertIn("registry.npmjs.org", warnings[0])
        self.assertIn("acme__widgets.toml", warnings[0])

    def test_warning_is_optional(self):
        cfg = self.load(self.body('[sandbox]\nverify_domains = ["pypi.org"]\n'))
        self.assertIsNotNone(cfg.probe_problem())

    def test_a_repo_that_never_verifies_has_nothing_to_check(self):
        text = self.body(
            '[verify]\nwhen = "never"\n[sandbox]\nverify_domains = ["pypi.org"]\n'
            '[preflight]\nprobe_url = "https://registry.npmjs.org/"\n'
        )
        warnings = []
        self.assertIsNone(self.load(text, log=warnings.append).probe_problem())
        self.assertEqual(warnings, [])

    def test_no_domains_means_no_check(self):
        warnings = []
        cfg = self.load(
            self.body('[preflight]\nprobe_url = "https://example.invalid/"\n'), log=warnings.append
        )
        self.assertEqual(warnings, [])
        self.assertIsNone(cfg.probe_problem())

    def test_defaults_are_consistent_with_each_other(self):
        warnings = []
        self.load(self.body('[sandbox]\nverify_domains = ["registry.npmjs.org"]\n'), log=warnings.append)
        self.assertEqual(warnings, [])


class StoragePaths(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        self.cfg = RepoConfig(REPO)

    def test_layout(self):
        paths = data_paths(self.cfg, self.data, env={})
        base = self.data / "repos" / "acme" / "widgets"
        self.assertEqual(paths.root, base)
        self.assertEqual(paths.clone, base / "clone")
        self.assertEqual(paths.runs, base / "runs")
        self.assertEqual(paths.run_dir("r1"), base / "runs" / "r1")

    def test_data_dir_comes_from_the_environment(self):
        paths = data_paths(self.cfg, env={"FACTORY_DATA": str(self.data)})
        self.assertEqual(paths.root, self.data / "repos" / "acme" / "widgets")

    def test_explicit_data_dir_beats_the_environment(self):
        paths = data_paths(self.cfg, self.data, env={"FACTORY_DATA": str(self.root / "other")})
        self.assertTrue(str(paths.root).startswith(str(self.data)))

    def test_hidden_paths_cover_other_repositories_the_config_dir_and_stray_data(self):
        for relative in ("repos/acme/widgets/clone", "repos/acme/gadgets/clone", "repos/other/tool/runs", "locks"):
            (self.data / relative).mkdir(parents=True)
        (self.data / "repos" / "acme" / "stray.txt").write_text("x")
        config = self.root / "config"
        config.mkdir()
        hidden = hidden_paths(self.cfg, self.data, config, env={})
        self.assertEqual(
            sorted(map(str, hidden)),
            sorted(
                str(path)
                for path in (
                    self.data / "locks",
                    self.data / "repos" / "acme" / "gadgets",
                    self.data / "repos" / "acme" / "stray.txt",
                    self.data / "repos" / "other" / "tool",
                    config,
                )
            ),
        )
        self.assertNotIn(self.data / "repos" / "acme" / "widgets", hidden)

    def test_hidden_paths_tolerate_a_data_dir_that_does_not_exist_yet(self):
        self.assertEqual(hidden_paths(self.cfg, self.data, None, env={}), ())

    def test_missing_data_dir(self):
        for env in ({}, {"FACTORY_DATA": ""}):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, "FACTORY_DATA"):
                data_paths(self.cfg, env=env)

    def test_relative_data_dir_becomes_absolute(self):
        self.assertTrue(data_paths(self.cfg, "rel/data", env={}).root.is_absolute())

    def test_dotdot_in_the_data_dir_is_refused(self):
        for value in (self.data / ".." / "x", "../x", "a/../../x"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r"\.\."):
                data_paths(self.cfg, value, env={})

    def test_odd_data_dir_values(self):
        for value in (b"/tmp/x", "a\x00b", 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data_paths(self.cfg, value, env={})

    def test_nothing_is_created_on_disk(self):
        data_paths(self.cfg, self.data, env={}).run_dir("r1")
        self.assertFalse(self.data.exists())

    def test_distinct_repos_never_share_a_directory(self):
        roots = {
            data_paths(RepoConfig(repo), self.data, env={}).root
            for repo in ("acme/widgets", "acme/gadgets", "other/widgets", "acme/.github", "acme/widgets.git")
        }
        self.assertEqual(len(roots), 5)

    def test_paths_stay_under_the_data_dir(self):
        for repo in ("acme/widgets", "acme/.github", "a.b/c.d"):
            root = data_paths(RepoConfig(repo), self.data, env={}).root
            self.assertEqual(root.relative_to(self.data).parts[0], "repos")
            self.assertNotIn("..", root.parts)

    def test_run_slugs_accepted(self):
        paths = data_paths(self.cfg, self.data, env={})
        for slug in ("r1", "R", "2026-11-06", "run_1", "a" * 50, "0", "a-b_c"):
            with self.subTest(slug):
                self.assertEqual(paths.run_dir(slug), paths.runs / slug)
                self.assertEqual(check_run_slug(slug), slug)

    def test_run_slugs_rejected_including_traversal(self):
        paths = data_paths(self.cfg, self.data, env={})
        for slug in (
            "",
            ".",
            "..",
            "../x",
            "x/..",
            "a/b",
            "/abs",
            "/",
            "\\x",
            "..\\x",
            "-x",
            "_x",
            ".hidden",
            "a b",
            "a.b",
            "a" * 51,
            "r1\n",
            "r1\x00",
            "ré",
            "١٢",
            "%2e%2e",
            "x/../../y",
            None,
            5,
            b"r1",
        ):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                paths.run_dir(slug)

    def test_run_slug_rule_matches_the_cli(self):
        # The CLI checks a run directory's basename with exactly this pattern
        # (fullmatch semantics, so a trailing newline does not slip through).
        self.assertEqual(config.RUN_SLUG.pattern, r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}")


class BaseBranchRule(ConfigTestCase):
    def test_matching_default_passes_silently(self):
        logged = []
        validate_base_branch(RepoConfig(REPO), "main", logged.append)
        validate_base_branch(RepoConfig(REPO, base_branch="trunk"), "trunk", logged.append)
        self.assertEqual(logged, [])

    def test_mismatch_is_refused_and_explains_why(self):
        cfg = RepoConfig(REPO, base_branch="develop")
        logged = []
        with self.assertRaises(ValueError) as caught:
            validate_base_branch(cfg, "main", logged.append)
        message = str(caught.exception)
        for fragment in ("develop", "main", "Closes #N", "allow_non_default_base"):
            self.assertIn(fragment, message)
        self.assertEqual(logged, [])

    def test_override_logs_exactly_once_and_passes(self):
        cfg = RepoConfig(REPO, base_branch="develop", allow_non_default_base=True)
        logged = []
        self.assertIsNone(validate_base_branch(cfg, "main", logged.append))
        self.assertEqual(len(logged), 1)
        self.assertIn("issues will stay open", logged[0])
        self.assertIn("develop", logged[0])

    def test_override_with_matching_default_has_nothing_to_report(self):
        logged = []
        validate_base_branch(RepoConfig(REPO, allow_non_default_base=True), "main", logged.append)
        self.assertEqual(logged, [])

    def test_log_defaults_to_a_noop(self):
        validate_base_branch(RepoConfig(REPO, base_branch="x", allow_non_default_base=True), "main")
        validate_base_branch(RepoConfig(REPO), "main")

    def test_case_differences_are_mismatches(self):
        with self.assertRaises(ValueError):
            validate_base_branch(RepoConfig(REPO, base_branch="Main"), "main")

    def test_default_branch_must_be_supplied(self):
        for value in (None, "", 5):
            with self.subTest(value), self.assertRaises(ValueError):
                validate_base_branch(RepoConfig(REPO), value)

    def test_default_base_is_main_and_an_override_comes_from_the_file(self):
        self.assertEqual(self.load(self.body()).base_branch, "main")
        cfg = self.load(self.body('base_branch = "develop"\nallow_non_default_base = true\n'))
        self.assertEqual((cfg.base_branch, cfg.allow_non_default_base), ("develop", True))
        with self.assertRaises(ValueError):
            validate_base_branch(self.load(self.body('base_branch = "develop"\n')), "main")




class LocalModeTests(unittest.TestCase):
    def test_empty_repo_is_local_mode_with_every_default(self):
        from factory.config import RepoConfig

        cfg = RepoConfig()
        self.assertEqual(cfg.repo, "")
        self.assertEqual(cfg.base_branch, "main")
        self.assertEqual(cfg.verify_when, "feature")
        self.assertEqual(cfg.acceptance_mode, "tests")
        self.assertEqual(cfg.artifact_roots, ())
        self.assertEqual(cfg.max_active_agents, 6)

    def test_loading_still_requires_a_real_org_name(self):
        from factory.config import load_repo_config

        for bad in ("", "name", "a/b/c", "../x/y"):
            with self.subTest(repo=bad), self.assertRaises(ValueError):
                load_repo_config(bad, config_dir="/nonexistent")


if __name__ == "__main__":
    unittest.main()
