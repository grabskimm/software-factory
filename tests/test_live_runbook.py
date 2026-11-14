"""docs/pod-backend-runbook.md: the live runbook (slice 21 of the pod backend, issues #144 to #147).

The runbook is a document, and nothing in it was run by its author. What these tests hold it to is what a document can be held to:
the parts and steps are in order and each step has its eight fields; the commands it names are commands the tools really have (each
is parsed with the tool's own argument parser, so a renamed option fails here); the hard gates, the mirror's separation rules and
the production role warning are in it; no command of it addresses production with a verb that writes; and the table that maps every
UNVERIFIED claim to the step that settles it still points at text that exists, and misses no UNVERIFIED paragraph, bullet or item of
the documents it covers.
"""

import importlib.util
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.live_tools import load

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "pod-backend-runbook.md"
TEXT = DOC.read_text(encoding="utf-8")
checks = load("live_checks")
reports = load("live_reports")
recording = load("live_recording")
probe = load("live_pod_probe")


PARTS = "ABCDEFG"
FIELDS = ("Needs", "Run", "Expect", "Stop if", "Rollback", "Approves", "Cost", "Record")
STEP_COUNTS = {"A": 7, "B": 8, "C": 6, "D": 9, "E": 7, "F": 4, "G": 9}


def normalise(text):
    return re.sub(r"\s+", " ", text).strip()


def sections():
    """{part letter: text of the part} and {step id: text of the step}."""
    parts, steps = {}, {}
    for match in re.finditer(r"^## Part ([A-G]): .*?(?=^## |\Z)", TEXT, re.MULTILINE | re.DOTALL):
        parts[match.group(1)] = match.group(0)
    for part, body in parts.items():
        for match in re.finditer(rf"^### ({part}\d+)\. (.*?)(?=^### |\Z)", body, re.MULTILINE | re.DOTALL):
            steps[match.group(1)] = match.group(0)
    return parts, steps


PART_TEXT, STEP_TEXT = sections()


class Structure(unittest.TestCase):
    def test_the_seven_parts_are_in_order(self):
        found = re.findall(r"^## Part ([A-G]): ", TEXT, re.MULTILINE)
        self.assertEqual(found, list(PARTS))

    def test_the_steps_of_each_part_are_numbered_without_a_gap(self):
        for part, count in STEP_COUNTS.items():
            ids = re.findall(rf"^### ({part}\d+)\. ", PART_TEXT[part], re.MULTILINE)
            self.assertEqual(ids, [f"{part}{n}" for n in range(1, count + 1)], part)

    def test_every_step_has_the_eight_fields_in_order(self):
        for step, body in STEP_TEXT.items():
            labels = re.findall(r"^\* \*\*([A-Za-z ]+):\*\*", body, re.MULTILINE)
            self.assertEqual(labels, list(FIELDS), step)
            for label in FIELDS:
                field = re.search(rf"^\* \*\*{label}:\*\*(.*?)(?=^\* \*\*|\Z)", body, re.MULTILINE | re.DOTALL).group(1)
                self.assertTrue(field.strip(), f"{step} {label} is empty")

    def test_the_record_field_names_only_kinds_of_the_vocabulary(self):
        for step, body in STEP_TEXT.items():
            field = re.search(r"^\* \*\*Record:\*\*(.*)$", body, re.MULTILINE).group(1)
            kinds = re.findall(r"`([a-z][a-z-]*)`", field)
            if not kinds:
                self.assertIn("none", field, step)
            for kind in kinds:
                self.assertIn(kind, recording.KINDS, f"{step} records a kind that does not exist: {kind}")

    def test_every_step_names_who_approves_and_what_it_costs_and_how_to_undo_it(self):
        for step, body in STEP_TEXT.items():
            approves = re.search(r"^\* \*\*Approves:\*\*(.*)$", body, re.MULTILINE).group(1)
            self.assertRegex(approves, r"Owner|Runner|Reviewer", step)
            rollback = re.search(r"^\* \*\*Rollback:\*\*(.*?)(?=^\* \*\*)", body, re.MULTILINE | re.DOTALL).group(1)
            self.assertTrue(rollback.strip().startswith("none") or len(rollback.strip()) > 20, step)

    def test_a_step_with_a_write_has_an_undo_that_is_not_none(self):
        for step, body in STEP_TEXT.items():
            run = re.search(r"^\* \*\*Run:\*\*(.*?)(?=^\* \*\*)", body, re.MULTILINE | re.DOTALL).group(1)
            rollback = re.search(r"^\* \*\*Rollback:\*\*(.*?)(?=^\* \*\*)", body, re.MULTILINE | re.DOTALL).group(1).strip()
            writes = re.search(r"terraform .*\bapply\b|terraform .*\bdestroy\b|kubectl .*\b(?:apply|create|delete|patch|run)\b|az group (?:create|delete)", run)
            if writes and not step.startswith(("A", "B")):
                self.assertFalse(rollback.startswith("none, read-only"), f"{step} writes and says it is read-only")


class Gates(unittest.TestCase):
    def test_part_f_is_the_plan_role_part_and_the_plan_as_the_plan_identity_is_a_hard_gate(self):
        part = PART_TEXT["F"]
        for needle in ("hard gate", "validatingadmissionpolicies", "validatingadmissionpolicybindings", "customresources/read", "admissionregistration", "never to widen the role by guessing"):
            self.assertIn(needle, part)
        f3 = STEP_TEXT["F3"]
        self.assertIn("HARD GATE", f3)
        for needle in ("terraform", "plan", "-lock=false", "AZURE_CONFIG_DIR", "plan-log", "--expect-no-changes", "Forbidden", "do not widen the role by guessing a name"):
            self.assertIn(needle, f3)
        self.assertIn("in writing", re.search(r"^\* \*\*Approves:\*\*(.*)$", f3, re.MULTILINE).group(1))
        fenced = "\n".join(re.findall(r"```(?:sh)?\n(.*?)```", f3, re.DOTALL))
        self.assertRegex(fenced, r"terraform -chdir=\S+ plan -lock=false -input=false")  # the flags of the plan job, in the command itself
        self.assertRegex(fenced, r'AZURE_CONFIG_DIR="\$WORK/az-plan"')

    def test_the_production_role_is_changed_in_exactly_one_step_after_the_gate(self):
        for step, body in STEP_TEXT.items():
            fenced = "\n".join(re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL))
            self.assertEqual("az role definition update" in fenced, step == "F4", step)
        f4 = STEP_TEXT["F4"]
        self.assertIn("F3", re.search(r"^\* \*\*Needs:\*\*(.*?)(?=^\* \*\*)", f4, re.MULTILINE | re.DOTALL).group(1))
        self.assertLess(TEXT.index("### F3."), TEXT.index("### F4."))
        approves = re.search(r"^\* \*\*Approves:\*\*(.*)$", f4, re.MULTILINE).group(1)
        self.assertIn("a production write: the production role", approves)
        self.assertIn("registry assignments of C4 and F1", approves)  # F4 is one of three production writes, not the only one

    def test_the_mirror_role_has_another_name_and_the_production_name_is_never_created_by_the_runbook(self):
        for step, body in STEP_TEXT.items():
            if "az role definition create" in "\n".join(re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL)):
                self.assertEqual(step, "F1")
        f1 = STEP_TEXT["F1"]
        self.assertIn("mirror-role", f1)
        self.assertIn("Do not", f1)
        self.assertIn("Custom role names are tenant-wide", STEP_TEXT["A4"])

    def test_the_prerequisites_state_one_repository_per_node_and_the_matrix(self):
        a2 = STEP_TEXT["A2"]
        self.assertIn("One repository per node", a2)
        self.assertIn("anti-affinity", a2)
        self.assertIn("tools/policy_matrix.py", a2)
        self.assertIn("kind", a2)
        self.assertIn("G6", a2)
        self.assertTrue(STEP_TEXT["G6"].startswith("### G6. One repository per node, live"))
        self.assertIn("colocation", STEP_TEXT["G6"])

    def test_the_mirror_has_its_own_prefix_state_group_and_secrets(self):
        a4 = STEP_TEXT["A4"]
        for needle in ("name_prefix", "resource group", "own Terraform state", "INFRA_TFVARS", "no pipeline runs for the mirror", "throwaway Anthropic key", "separate workspace", "quota"):
            self.assertIn(needle, a4)
        self.assertIn("state list", STEP_TEXT["C3"])

    def test_part_g_names_the_slices_it_needs_and_ends_with_the_teardown(self):
        g = PART_TEXT["G"]
        for needle in ("12b", "16", "17", "is **not run**"):
            self.assertIn(needle, g)
        self.assertTrue(STEP_TEXT["G9"].startswith("### G9. Teardown"))
        self.assertIn("always run", STEP_TEXT["G9"])

    def test_the_default_off_promise_is_in_the_first_part(self):
        self.assertIn("Nothing in this runbook sets", STEP_TEXT["A2"])
        for variable in ("pod_backend_enabled", "worker_nodepool_enabled", "network_data_plane", "nap_mode"):
            self.assertIn(variable, STEP_TEXT["A2"])


def code_blocks():
    return re.findall(r"```(?:sh)?\n(.*?)```", TEXT, re.DOTALL)


def logical_lines(block):
    """The commands of a code block: backslash continuations joined, heredoc bodies and comments dropped."""
    lines, current, heredoc = [], "", None
    for raw in block.splitlines():
        line = raw.strip()
        if heredoc:
            if line == heredoc:
                heredoc = None
            continue
        if not line or line.startswith("#"):
            continue
        current += (" " if current else "") + line.rstrip("\\").strip()
        if raw.rstrip().endswith("\\"):
            continue
        match = re.search(r"<<'?(\w+)'?\s*$|<<'?(\w+)'?\s*\|", current)
        if match and not current.rstrip().endswith("<<"):
            heredoc = match.group(1) or match.group(2)
        lines.append(current)
        current = ""
    return lines


def command_texts():
    """(text, inline) for every command line of the page: the logical lines of the code blocks, the inline code spans and the `$(...)` substitutions."""
    texts = [(line, False) for block in code_blocks() for line in logical_lines(block)]
    texts += [(span, True) for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", TEXT)]
    texts += [(span, False) for span in re.findall(r"\$\(([^()]+)\)", TEXT)]
    return texts


def after_guards(tokens):
    """The tokens after a leading chain of the guards of C1 and G9 (`mirror_guard && [ ... ] && command`): the command is what is parsed."""
    while "&&" in tokens:
        head = tokens[: tokens.index("&&")]
        if not head or not (head[0].startswith("mirror_") or head[0] in ("[", "[[")):
            break
        tokens = tokens[tokens.index("&&") + 1 :]
    return tokens


def program_start(tokens):
    """The index of the first word that is not a `NAME=value` assignment."""
    index = 0
    while index < len(tokens) and re.fullmatch(r"[A-Za-z_]+=\S*", tokens[index]):
        index += 1
    return index


def commands(prefix):
    """The argv tokens (after the program word) of every command that runs the shell function or program `prefix`.

    A shell function (`live`) is the program word of the line; `python3 -` (a probe fed to an interpreter) and `tools/policy_matrix.py` may follow `kubectl exec ... --` or `python3`.
    A sentence that merely mentions a command (an inline span with nothing after it, or the word `live` inside prose) is not one."""
    found = []
    words = prefix.split()
    for line, inline in command_texts():
        line = re.split(r"\s+(?:\||2?>|<)\s+|\s+<\s*\"", line)[0] if prefix != "python3 -" else line
        try:
            tokens = shlex.split(line, comments=False, posix=True)
        except ValueError:
            continue
        tokens = after_guards(tokens)
        starts = [program_start(tokens)] if prefix in ("live", "reports", "record") else range(len(tokens) - len(words) + 1)
        for index in starts:
            if tokens[index : index + len(words)] != words:
                continue
            rest = tokens[index + len(words) :]
            if not rest or "<<command>>" in rest or "<<KIND>>" in rest or rest[0].startswith("<<") or (inline and len(rest) == 1):
                break  # a sentence about the form of a command, not a command
            for stop, token in enumerate(rest):  # a redirection, a pipe, a loop body or a chain ends the command
                if token.endswith(";") or token in ("&&", "done", ";", "<", ">", "2>", "|"):
                    rest = rest[:stop] + ([token[:-1]] if token.endswith(";") and len(token) > 1 else [])
                    break
            found.append(rest)
            break
    return found


def parse_with(module, tokens):
    import contextlib
    import io

    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return module.parse_arguments([t if not t.startswith("$(") else "x" for t in tokens])
    except SystemExit:
        raise AssertionError(f"the tool refuses {tokens}: {err.getvalue().strip().splitlines()[-1:]}") from None


class Commands(unittest.TestCase):
    def test_every_live_command_of_the_runbook_parses_with_the_tools_own_parser(self):
        seen = set()
        for tokens in commands("live"):
            args = parse_with(checks, tokens)
            seen.add(args.command)
        self.assertEqual(seen, set(checks.COMMANDS), "a command of live_checks.py is never used by the runbook, or one is missing")

    def test_every_report_command_of_the_runbook_parses_and_every_report_is_used(self):
        seen = {parse_with(reports, tokens).command for tokens in commands("reports")}
        self.assertEqual(seen, set(reports.COMMANDS))

    def test_every_recording_command_parses_and_both_are_used(self):
        seen = {parse_with(recording, tokens).command for tokens in commands("record")}
        self.assertEqual(seen, {"import", "validate"})
        for tokens in commands("record"):
            args = parse_with(recording, tokens)
            if args.command == "import":
                self.assertRegex(args.step, r"^[A-G][0-9]{1,2}$")

    def test_every_probe_mode_of_the_runbook_parses_and_every_mode_is_used(self):
        modes = set()
        for tokens in commands("python3 -"):
            modes.add(parse_with(probe, tokens).mode)
        self.assertEqual(modes, {"socket", "spool", "secret-volume", "environment", "network", "statuses"})

    def test_the_matrix_commands_parse_with_the_matrix_tools_parser(self):
        spec = importlib.util.spec_from_file_location("policy_matrix_for_runbook_test", ROOT / "tools" / "policy_matrix.py")
        matrix = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = matrix
        spec.loader.exec_module(matrix)
        found = [tokens for tokens in commands("tools/policy_matrix.py")]
        self.assertEqual(sorted(parse_with(matrix, tokens).command for tokens in found), ["run", "status"])

    def test_the_steps_that_record_use_a_step_id_of_their_own(self):
        for step, body in STEP_TEXT.items():
            for match in re.finditer(r"--step ([A-G][0-9]+)", body):
                self.assertEqual(match.group(1), step, f"{step} records under another step's name")

    def test_the_tools_the_runbook_names_exist(self):
        for name in re.findall(r"tools/(live_[a-z_]+\.py)", TEXT):
            self.assertTrue((ROOT / "tools" / name).is_file(), name)
        for name in ("live_checks", "live_reports", "live_recording", "live_pod_probe"):
            self.assertIn(f"tools/{name}.py", TEXT)


class Safety(unittest.TestCase):
    def test_a_kubectl_command_always_names_its_cluster_in_a_block_in_a_sentence_and_in_a_substitution(self):
        # The page's own rule (A4) is "never kubectl without one". It used to be checked in the fenced blocks only, and the commands written
        # in a sentence (`kubectl -n factory rollout restart deploy/factory`, `kubectl delete node`) carried none: they would have acted on whatever the
        # default context is, which may be production. Every command text of the page is checked: blocks, inline spans and `$(...)`.
        verb = re.compile(r"\bkubectl\s+(?:(?:-n|--namespace|--as|-l)\s+\S+\s+)*(?:get|apply|create|delete|exec|run|patch|describe|logs|wait|explain|version|rollout|auth|label|annotate|scale|drain|cordon|uncordon|cp|edit|replace|top|set|taint|expose|port-forward|config)\b")
        found = 0
        for text, _inline in command_texts():
            if verb.search(text) or re.match(r"^(?:[A-Z_]+=\S+ )*(?:until |while .* )?kubectl\b\s+\S", text):
                found += 1
                self.assertIn("--kubeconfig", text, text)
        self.assertGreater(found, 60)  # the scan sees the commands: it is not an empty loop

    def test_a_write_verb_of_kubectl_without_a_kubeconfig_is_found_by_the_scan(self):
        # the scan itself, on the commands the review found in prose
        verb = re.compile(r"\bkubectl\s+(?:(?:-n|--namespace)\s+\S+\s+)*(?:delete|apply|create|patch|exec|rollout)\b")
        for text in ("kubectl -n factory rollout restart deploy/factory", "kubectl delete node", "kubectl create namespace mirror-pool", "kubectl -n factory-workers delete networkpolicy default-deny"):
            self.assertIsNotNone(verb.search(text), text)
            self.assertNotIn("--kubeconfig", text)

    def test_no_kubectl_verb_stands_alone_in_a_sentence_as_a_command(self):
        for text, inline in command_texts():
            if inline:
                self.assertNotRegex(text, r"^kubectl (?:exec|delete|patch|apply|create|get)$", text)

    def test_a_live_command_that_calls_kubectl_carries_a_kubeconfig(self):
        seen = 0
        for tokens in commands("live"):
            args = parse_with(checks, tokens)
            if checks.uses_kubectl(args):
                seen += 1
                self.assertIsNotNone(args.kubeconfig, f"live {' '.join(tokens)} would use the default cluster, and the tool refuses it")
        self.assertGreater(seen, 12)

    def test_a_live_command_written_in_a_sentence_names_its_cluster_too(self):
        # the parser test above skips a span that is only `live nodes` (a sentence about the form of a command); the page must not leave out the flag there either
        needs = re.compile(r"(?:^|\s)live\s+(?:--\S+\s+\S+\s+)*(?:nodes|node-requests|crds|reads|can-i|colocation|pod-fields|denial-sample|capture(?!\s+--kind az-aks))\b")
        found = 0
        for text, _inline in command_texts():
            if needs.search(text):
                found += 1
                self.assertIn("--kubeconfig", text, text)
        self.assertGreater(found, 12)

    def test_an_az_write_in_a_sentence_names_the_mirror_by_its_variable(self):
        write = re.compile(r"\baz\s+(?:group|role|ad|storage|keyvault|aks|monitor|network)\b.*\s(?:create|delete|update|purge)\b")
        for text, inline in command_texts():
            if inline and write.search(text) and " --" in text:
                self.assertRegex(text, r'\$MIRROR_|\$APP_ID|\$ROLE_FILE|mirror_', text)

    def test_nothing_that_writes_ever_runs_against_the_production_kubeconfig(self):
        writing = re.compile(r"\b(?:apply|create|delete|patch|replace|edit|exec|run|scale|annotate|label|drain|cordon|cp)\b")
        for block in code_blocks():
            for line in logical_lines(block):
                if "KC_PROD" in line and "kubectl" in line:
                    self.assertIsNone(writing.search(line.split("KC_PROD", 1)[1]), line)
        self.assertNotRegex(TEXT, r"terraform[^\n]*-auto-approve")

    def test_no_secret_a_guid_or_a_real_address_is_in_the_page(self):
        self.assertIsNone(recording.GUID.search(TEXT))
        self.assertNotRegex(TEXT, r"sk-ant-|ghp_|-----BEGIN|eyJ[A-Za-z0-9_-]{10,}")
        for address in re.findall(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])", TEXT):
            self.assertIn(address, ("0.0.0.0", "169.254.169.254", "168.63.129.16"))

    def test_the_forbidden_wording_of_the_network_policy_list_is_not_in_the_page(self):
        self.assertNotRegex(TEXT, r"(?i)policy\s+(?:#\s*)?engine|issue #?122(?!\d)")

    def test_the_secret_it_does_handle_never_goes_in_a_file_or_an_argument(self):
        f1 = STEP_TEXT["F1"]
        self.assertIn("one shell's environment", f1)
        self.assertIn('unset PLAN_SECRET', f1)
        self.assertNotRegex(TEXT, r"--password ['\"][^$]")

    def test_every_placeholder_is_in_the_double_angle_form(self):
        for line in TEXT.splitlines():
            if line.startswith("|") or "`<<" in line and ">>`" in line:
                continue
            self.assertNotRegex(re.sub(r"<<[A-Z0-9_]+>>", "", line.replace("<<'EOF'", "").replace("<<EOF", "")), r"<[A-Z][A-Z0-9_]{3,}>", line)


BASH = shutil.which("bash")


def fenced_blocks(step):
    return re.findall(r"```(?:sh)?\n(.*?)```", STEP_TEXT[step], re.DOTALL)


def guard_functions():
    """The text of the four shell functions of C1, exactly as the page has them (the page's own code is what is run by the tests)."""
    block = textwrap.dedent(fenced_blocks("C1")[0])
    start = block.index("mirror_guard() {")
    end = block.index("{ declare -p")
    return block[start:end]


GOOD_NAMES = {
    "PROD_RG": "rg-factory", "PROD_PREFIX": "fac", "PROD_STATE_CONTAINER": "tfstate",
    "MIRROR_PREFIX": "pbmir", "MIRROR_RG": "rg-pbmir", "MIRROR_STATE_CONTAINER": "tfstate-pbmir",
}


def run_bash(script, env=None, az=""):
    """(exit status, stdout, stderr) of a bash script that has the page's functions, the given names and a stub `az`."""
    names = dict(GOOD_NAMES, **(env or {}))
    exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in names.items())
    program = f"{exports}\naz() {{ {az or 'return 0'}; }}\n{guard_functions()}\n{script}\n"
    done = subprocess.run([BASH, "-c", program], capture_output=True, text=True, timeout=20, check=False)
    return done.returncode, done.stdout, done.stderr


@unittest.skipUnless(BASH, "bash is needed to run the guards of the page")
class Guards(unittest.TestCase):
    """The machine guards of C1 (shell functions on the page). A mirror name that is production's, empty or unrelated to the prefix is refused by the machine, before any create or delete."""

    def test_the_names_of_a_proper_mirror_are_accepted(self):
        self.assertEqual(run_bash("mirror_guard")[0], 0)

    def test_an_empty_name_is_refused_each_of_the_six(self):
        for key in GOOD_NAMES:
            with self.subTest(key):
                code, _, err = run_bash("mirror_guard", {key: ""})
                self.assertEqual(code, 1)
                self.assertIn("is empty", err)

    def test_a_mirror_name_that_is_production_s_is_refused_naming_which(self):
        # each case is otherwise a perfectly good mirror: only the one name is production's
        cases = (
            ({"PROD_PREFIX": "pbmir"}, "the mirror prefix is production's"),
            ({"PROD_RG": "rg-pbmir"}, "the mirror group is production's"),
            ({"PROD_STATE_CONTAINER": "tfstate-pbmir"}, "the mirror state container is production's"),
        )
        for env, message in cases:
            with self.subTest(env):
                code, _, err = run_bash("mirror_guard", env)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)

    def test_the_group_and_the_state_container_must_hold_the_prefix(self):
        for env in ({"MIRROR_RG": "rg-other"}, {"MIRROR_STATE_CONTAINER": "tfstate-other"}):
            code, _, err = run_bash("mirror_guard", env)
            self.assertEqual(code, 1, env)
            self.assertIn("does not hold the mirror prefix", err)

    def test_a_prefix_that_is_not_a_name_prefix_is_refused(self):
        for prefix in ("PBMIR", "1abc", "a--b", "a", "abcdefghijklm", "abc-", "ab c", "ab;c", "ab$c"):
            with self.subTest(prefix):
                code, _, err = run_bash("mirror_guard", {"MIRROR_PREFIX": prefix, "MIRROR_RG": f"rg-{prefix}", "MIRROR_STATE_CONTAINER": f"tf-{prefix}"})
                self.assertEqual(code, 1, err)
        self.assertEqual(run_bash("mirror_guard", {"MIRROR_PREFIX": "ab", "MIRROR_RG": "rg-ab", "MIRROR_STATE_CONTAINER": "tf-ab"})[0], 0)
        self.assertEqual(run_bash("mirror_guard", {"MIRROR_PREFIX": "a-b-c", "MIRROR_RG": "rg-a-b-c", "MIRROR_STATE_CONTAINER": "tf-a-b-c"})[0], 0)

    def test_the_group_is_the_mirror_s_only_when_the_tag_says_so_and_the_tag_is_not_asked_when_the_names_are_refused(self):
        self.assertEqual(run_bash("mirror_group_is_ours", az="echo pod-backend-mirror")[0], 0)
        for tag in ("", "production", "pod-backend-mirror-x", "null"):
            with self.subTest(tag):
                code, _, err = run_bash("mirror_group_is_ours", az=f"echo {shlex.quote(tag)}")
                self.assertEqual(code, 1)
                self.assertIn("refused: the group is not the mirror's", err)
        with tempfile.TemporaryDirectory() as directory:
            marker = f"{directory}/called"
            code, _, _ = run_bash("mirror_group_is_ours", {"MIRROR_RG": "rg-factory", "PROD_RG": "rg-factory"}, az=f"touch {marker}; echo pod-backend-mirror")
            self.assertEqual(code, 1)
            self.assertFalse(Path(marker).exists(), "a refused name must not even reach az")

    def test_a_guarded_command_does_not_run_when_the_guard_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = f"{directory}/ran"
            code, _, _ = run_bash(f"mirror_group_is_ours && touch {marker}", {"MIRROR_RG": "rg-factory", "PROD_RG": "rg-factory"}, az="echo pod-backend-mirror")
            self.assertEqual(code, 1)
            self.assertFalse(Path(marker).exists())
            code, _, _ = run_bash(f"mirror_group_is_ours && touch {marker}", az="echo pod-backend-mirror")
            self.assertEqual(code, 0)
            self.assertTrue(Path(marker).exists())

    def test_the_tfvars_must_name_the_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            def check(text):
                Path(directory, "mirror.tfvars").write_text(text)
                return run_bash("mirror_tfvars_ok", {"WORK": directory}, az="")[0]

            example = 'resource_group_name = "rg-pbmir"\nname_prefix         = "pbmir" # 2-12 characters\n'
            self.assertEqual(check(example), 0)
            self.assertEqual(check(example.replace("pbmir", "fac")), 1)                                   # production's prefix
            self.assertEqual(check('resource_group_name = "rg-factory"\nname_prefix = "pbmir"\n'), 1)    # production's group
            self.assertEqual(check('name_prefix = "pbmir2"\nresource_group_name = "rg-pbmir"\n'), 1)      # a longer prefix is not the prefix
            self.assertEqual(check('name_prefix = "<<NAME_PREFIX>>"\nresource_group_name = "<<RESOURCE_GROUP>>"\n'), 1)  # the example, not edited
            self.assertEqual(check('name_prefix = "pbmir"\n'), 1)

    def test_the_application_is_the_mirror_s_only_when_its_display_name_says_so(self):
        self.assertEqual(run_bash("mirror_app_is_ours", {"APP_ID": "x"}, az="echo sp-plan-pbmir")[0], 0)
        for name in ("sp-plan-fac", "someone-else", ""):
            self.assertEqual(run_bash("mirror_app_is_ours", {"APP_ID": "x"}, az=f"echo {name}")[0], 1, name)
        self.assertEqual(run_bash("mirror_app_is_ours", {"APP_ID": ""}, az="echo sp-plan-pbmir")[0], 1)  # an empty id must never reach `az ad app delete`

    def test_the_names_file_the_page_writes_carries_the_functions_to_a_new_shell(self):
        block = textwrap.dedent(fenced_blocks("C1")[0])
        self.assertIn('declare -f mirror_guard mirror_group_is_ours mirror_tfvars_ok mirror_app_is_ours', block)
        with tempfile.TemporaryDirectory() as directory:
            names = f"{directory}/mirror-names.sh"
            export = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in GOOD_NAMES.items())
            writer = f"{export}\n{guard_functions()}\n{{ declare -p {' '.join(GOOD_NAMES)}; declare -f mirror_guard mirror_group_is_ours mirror_tfvars_ok mirror_app_is_ours; }} > {names}\n"
            self.assertEqual(subprocess.run([BASH, "-c", writer], capture_output=True, text=True, timeout=20).returncode, 0)
            fresh = subprocess.run([BASH, "-c", f'. {names}; mirror_guard && echo accepted; MIRROR_RG=rg-factory PROD_RG=rg-factory; mirror_guard || echo refused'], capture_output=True, text=True, timeout=20, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(fresh.stdout.split(), ["accepted", "refused"], fresh.stderr)

    def test_every_block_of_the_page_is_valid_bash_once_its_placeholders_are_names(self):
        checked = 0
        for block in code_blocks():
            text = re.sub(r"<<([A-Z0-9_]+)>>", r"PLACEHOLDER_\1", textwrap.dedent(block))
            done = subprocess.run([BASH, "-n"], input=text, capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(done.returncode, 0, f"{done.stderr.strip()[:200]}\n{text[:200]}")
            checked += 1
        self.assertGreater(checked, 30)

    def test_every_creating_or_deleting_az_command_of_the_blocks_starts_with_a_guard(self):
        creating = re.compile(r"\baz\s+(?:group (?:create|delete)|role definition (?:create|delete)|role assignment (?:create|delete)|ad app (?:create|delete)|ad sp create|ad app credential reset|storage container delete)\b")
        found = 0
        for step, body in STEP_TEXT.items():
            for block in re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL):
                for line in logical_lines(block):
                    if not creating.search(line) or line.startswith(("APP_ID=", "PLAN_SECRET=")):
                        continue
                    found += 1
                    self.assertRegex(line, r"^(?:\[ -n \"\$\w+\" \] && )*mirror_(?:guard|group_is_ours|app_is_ours) && ", f"{step}: {line}")
        for step, body in STEP_TEXT.items():  # the same two that capture an id for later use
            for block in re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL):
                for line in logical_lines(block):
                    if line.startswith(("APP_ID=", "PLAN_SECRET=")):
                        self.assertIn("mirror_", line)
        self.assertGreaterEqual(found, 10)


def step_field(step, label):
    return re.search(rf"^\* \*\*{label}:\*\*(.*?)(?=^\* \*\*|\Z)", STEP_TEXT[step], re.MULTILINE | re.DOTALL).group(1)


class ProductionWrites(unittest.TestCase):
    """The mirror shares the registry with production and writes two role assignments on it. Both are production writes: declared, approved by the Owner, and deleted by name in the teardown."""

    def test_the_infrastructure_really_assigns_acr_pull_on_the_production_registry_and_only_that_one_on_it(self):
        text = (ROOT / "infra" / "identity.tf").read_text(encoding="utf-8")
        block = re.search(r'resource "azurerm_role_assignment" "acr_pull" \{(.*?)\n\}', text, re.DOTALL).group(1)
        self.assertIn("data.azurerm_container_registry.this.id", block)
        self.assertIn('"AcrPull"', block)
        self.assertIn("kubelet_identity", block)
        self.assertEqual(len(re.findall(r"scope\s*=\s*data\.azurerm_container_registry\.this\.id", text)), 1)

    def test_every_production_write_is_named_in_the_header_the_roles_and_the_stop_rule(self):
        header = TEXT[: TEXT.index("## How to read this page")] + TEXT[TEXT.index("## How to read this page") : TEXT.index("## Part A")]
        self.assertIn("three production writes", header)
        self.assertIn("AcrPull", header)
        owner_row = next(line for line in step_field("A1", "Run").splitlines() if "| Owner |" in line)
        self.assertIn("registry", owner_row)
        stop = step_field("A1", "Stop if")
        for step in ("C4", "F1", "F4"):
            self.assertIn(step, stop)
        # the claim that F4 is the only production write was false, and is not made anywhere
        self.assertNotRegex(TEXT, r"(?i)only F4|F4 is the only|the only one of this runbook")

    def test_the_two_registry_steps_need_the_owner_in_writing_and_say_what_they_write(self):
        c4, f1 = STEP_TEXT["C4"], STEP_TEXT["F1"]
        self.assertRegex(step_field("C4", "Approves"), r"Owner")
        self.assertRegex(step_field("F1", "Approves"), r"Owner")
        for step in ("C4", "F1"):
            needs = step_field(step, "Needs")
            self.assertIn("production registry", needs, step)
            self.assertIn("approval", needs, step)
            self.assertIn("G9", needs, step)
        self.assertIn("AcrPull", c4)
        self.assertIn("azurerm_role_assignment.acr_pull", c4)
        self.assertIn('--role "Reader" --scope "$(az acr show', f1)
        self.assertIn("PRODUCTION write", f1)
        a4 = STEP_TEXT["A4"]
        self.assertIn("Both are production writes", a4)
        self.assertIn("does not", a4[a4.index("Both are production writes") :][:300])

    def test_no_other_step_assigns_a_role_on_the_registry(self):
        for step, body in STEP_TEXT.items():
            for block in re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL):
                for line in logical_lines(block):
                    if re.search(r"az role assignment create.*acr show", line):
                        self.assertEqual(step, "F1")

    def test_the_teardown_deletes_both_assignments_by_name_before_the_group_and_the_destroy_and_says_the_group_delete_does_not(self):
        g9 = STEP_TEXT["G9"]
        block = "\n".join(fenced_blocks("G9"))
        lines = logical_lines(block)
        deletes = [line for line in lines if "az role assignment delete" in line]
        self.assertEqual(len(deletes), 2)
        self.assertTrue(any("AcrPull" in line and "$KUBELET_OID" in line for line in deletes))
        self.assertTrue(any("--role Reader" in line and "$APP_ID" in line for line in deletes))
        for line in deletes:
            self.assertRegex(line, r'^\[ -n "\$ACR_ID" \]')
            for needle in ("--assignee", "--role", '--scope "$ACR_ID"', "mirror_"):
                self.assertIn(needle, line)
            self.assertRegex(line, r'\[ -n "\$(?:KUBELET_OID|APP_ID)" \]|mirror_app_is_ours')
        position = lambda needle: next(i for i, line in enumerate(lines) if needle in line)  # noqa: E731
        for later in ("az group delete", "terraform -chdir", "az role definition delete", "az ad app delete"):
            self.assertLess(max(position("az role assignment delete --assignee \"$KUBELET_OID\""), position("--role Reader")), position(later), later)
        self.assertIn("kubelet identity still exists", block)
        self.assertIn("Deleting the resource group does not remove the two assignments on the registry", g9)
        self.assertIn("no assignment of the mirror remains on the registry", g9)
        self.assertRegex(block, r'az role assignment list --scope "\$ACR_ID" --assignee "\$KUBELET_OID"')
        self.assertRegex(block, r'az role assignment list --scope "\$ACR_ID" --assignee "\$APP_ID"')

    def test_an_empty_assignee_could_delete_every_assignment_of_the_registry_so_each_delete_tests_it(self):
        # `az role assignment delete --scope S` with no assignee and no role deletes every assignment at S
        for line in logical_lines("\n".join(fenced_blocks("G9"))):
            if "az role assignment delete" in line:
                self.assertRegex(line, r"--assignee \"\$\w+\"")
                self.assertRegex(line, r"--role \S+")


class ScratchCopy(unittest.TestCase):
    def test_the_scratch_copy_takes_the_committed_files_only(self):
        c1 = "\n".join(fenced_blocks("C1"))
        self.assertIn('git -C "$TREE" archive HEAD infra | tar -x -C "$WORK/infra-mirror" --strip-components=1', c1)
        self.assertNotRegex(TEXT, r'cp -r "\$TREE/infra"')
        self.assertNotRegex(TEXT, r"cp -[a-zA-Z]*r[a-zA-Z]* \S*infra")

    def test_a_stop_rule_finds_the_files_terraform_would_load_silently(self):
        c1 = "\n".join(fenced_blocks("C1"))
        find = next(line for line in logical_lines(c1) if line.startswith("find "))
        for needle in ("*.tfstate*", "override.tf*", "*_override.tf*", "*.tfvars", "*.tfvars.json", "! -name '*.auto.tfvars'", "-print"):
            self.assertIn(needle, find)
        self.assertIn("prints **nothing**", step_field("C1", "Expect"))
        self.assertIn("`find` prints any path", step_field("C1", "Stop if"))

    def test_the_find_of_the_page_finds_what_a_first_apply_leaves_and_leaves_the_committed_auto_files(self):
        if not BASH:
            self.skipTest("bash")
        find = next(line for line in logical_lines("\n".join(fenced_blocks("C1"))) if line.startswith("find "))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "infra-mirror")
            root.mkdir()
            for name in ("image.auto.tfvars", "ingress.auto.tfvars", "main.tf", "terraform.tfvars.example"):
                (root / name).write_text("x")
            def found():
                done = subprocess.run([BASH, "-c", find], capture_output=True, text=True, timeout=20, env={"WORK": directory, "PATH": "/usr/bin:/bin"})
                return done.stdout.split()
            self.assertEqual(found(), [])  # a clean copy prints nothing
            for name in ("terraform.tfvars", "prod.tfvars.json", "terraform.tfstate", "terraform.tfstate.backup", "override.tf", "x_override.tf", "override.tf.json"):
                (root / name).write_text("x")
                self.assertEqual(found(), [str(root / name)], name)
                (root / name).unlink()
            (root / ".terraform").mkdir()
            self.assertEqual(found(), [str(root / ".terraform")])

    def test_the_group_is_created_only_when_it_does_not_exist_and_a_refusal_is_a_stop(self):
        c1 = "\n".join(fenced_blocks("C1"))
        create = next(line for line in logical_lines(c1) if "az group create" in line)
        self.assertRegex(create, r'^mirror_guard && \[ "\$\(az group exists --name "\$MIRROR_RG"\)" = false \] && az group create ')
        self.assertIn('--tags purpose=pod-backend-mirror', create)
        stop = step_field("C1", "Stop if")
        self.assertIn("`az group exists` prints `true`", stop)
        self.assertIn("do **not** create over it", stop)
        self.assertIn("mirror_group_is_ours && az group delete", step_field("C1", "Rollback"))

    def test_g9_checks_the_tag_before_it_deletes_the_group_and_before_the_terraform_destroy(self):
        lines = logical_lines("\n".join(fenced_blocks("G9")))
        deleting = [line for line in lines if "az group delete" in line or re.search(r"terraform .*\bdestroy\b", line)]
        self.assertEqual(len(deleting), 2)
        for line in deleting:
            self.assertTrue(line.startswith("mirror_group_is_ours && "), line)
        self.assertIn("tags.purpose", "\n".join(fenced_blocks("C1")))
        self.assertIn("pod-backend-mirror", "\n".join(fenced_blocks("C1")))

    def test_terraform_that_writes_the_mirror_is_guarded_in_c3_c4_and_e1(self):
        for step in ("C3", "C4", "E1"):
            applies = [line for block in fenced_blocks(step) for line in logical_lines(block) if re.search(r"terraform .*\bapply\b", line)]
            self.assertTrue(applies, step)
            for line in applies:
                self.assertTrue(line.startswith("mirror_guard && mirror_tfvars_ok && "), (step, line))

    def test_the_names_the_page_uses_are_variables_after_c1_not_pasted_placeholders(self):
        after = TEXT[TEXT.index("### C2.") :]
        before_the_table = after[: after.index("## The recording format")]
        for placeholder in ("<<MIRROR_RG>>", "<<MIRROR_PREFIX>>", "<<MIRROR_STATE_CONTAINER>>"):
            self.assertNotIn(placeholder, before_the_table)
        self.assertIn("<<MIRROR_RG>>", step_field("C1", "Run"))


class AgainstTheTree(unittest.TestCase):
    """Part G is read against the slices as they are in the tree: the names it uses are the ones of the Terraform and the contract."""

    POD_BACKEND = (ROOT / "infra" / "pod_backend.tf").read_text(encoding="utf-8")
    RBAC = (ROOT / "infra" / "worker_rbac.tf").read_text(encoding="utf-8")

    def test_the_container_names_of_the_manager_pod_are_those_of_the_terraform(self):
        names = dict(re.findall(r"^\s+(init|launcher|relay)\s+= \"([a-z-]+)\"$", self.POD_BACKEND, re.MULTILINE))
        self.assertEqual(names, {"init": "volume-perms", "launcher": "launcher", "relay": "relay"})
        g = PART_TEXT["G"]
        for name in names.values():
            self.assertIn(f"`{name}`", g)
        used = set(re.findall(r"exec -i deploy/factory -c (\w+)|\$P -c (\w+)", STEP_TEXT["G2"] + STEP_TEXT["G3"]))
        containers = {a or b for a, b in used}
        self.assertEqual(containers, {"factory", "launcher", "relay"})
        self.assertIn('name            = "factory"', (ROOT / "infra" / "k8s.tf").read_text(encoding="utf-8"))

    def test_the_role_the_runbook_names_is_the_one_the_terraform_makes_with_the_verbs_the_checks_expect(self):
        self.assertIn('worker_launcher_role_name = "factory-launcher"', self.RBAC)
        g1 = STEP_TEXT["G1"]
        self.assertIn("role,rolebinding factory-launcher", g1)
        self.assertIn("`factory-launcher`", PART_TEXT["G"])
        self.assertIn('verbs      = ["create", "get", "list", "delete"]', self.RBAC)
        self.assertIn('verbs      = ["create", "delete"]', self.RBAC)
        # what live_checks expects of the launcher is exactly what the Role grants
        granted = {("pods", verb) for verb in ("create", "get", "list", "delete")} | {("secrets", verb) for verb in ("create", "delete")}
        expected_yes = {(resource, verb) for verb, resource, namespace, want in checks.LAUNCHER_CAN if want == "yes" and namespace == checks.WORKER_NS}
        self.assertEqual(expected_yes, granted)

    def test_the_settings_the_page_says_come_with_the_flip_are_the_contracts(self):
        from factory import podcontract as pc

        g = PART_TEXT["G"]
        for name in (*pc.ENV_MANAGER.values(),):
            self.assertIn(name, g)
        self.assertIn("pod_backend_enabled", g)
        self.assertIn("never set by hand", g)
        self.assertIn('"factory-relay-svc"' if False else "factory-relay-svc", g)
        self.assertIn('pod_backend_relay_service_name = "${local.k8s_app_name}-relay-svc"', self.POD_BACKEND)
        for uid in (pc.SUPERVISOR_UID, pc.WORKER_UID, pc.WORKER_GID, pc.LAUNCHER_UID, pc.RELAY_UID):
            self.assertIn(str(uid), g)

    def test_the_supervisor_probe_runs_as_the_supervisor_and_the_worker_probes_as_the_worker(self):
        from factory import podcontract as pc

        lines = [line for line in logical_lines("\n".join(fenced_blocks("G2"))) if "live_pod_probe.py" in line]
        sockets = [line for line in lines if " socket " in line]
        self.assertEqual(len(sockets), 2)
        for line in lines:
            if " socket " in line or " spool " in line:
                tokens = shlex.split(line.split("<")[0])
                uid, gid = tokens[tokens.index("--as-uid") + 1], tokens[tokens.index("--as-gid") + 1]  # every identity probe names its identity
                if "--expect supervisor" in line:
                    self.assertEqual((uid, gid), (str(pc.SUPERVISOR_UID), str(pc.SHARED_GID)), line)
                else:
                    self.assertEqual((uid, gid), (str(pc.WORKER_UID), str(pc.WORKER_GID)), line)
        self.assertIn("as the supervisor (uid 10001", step_field("G2", "Expect"))
        self.assertIn("names its own uid and gid", step_field("G2", "Run"))

    def test_the_full_launcher_check_is_g1_after_the_role_exists_and_e4_has_only_create(self):
        for step, body in STEP_TEXT.items():
            fenced = "\n".join(re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL))
            self.assertEqual("can-i --profile launcher" in fenced, step == "G1", step)
        e4 = "\n".join(fenced_blocks("E4"))
        self.assertIn("auth can-i create pods -n factory-workers --as system:serviceaccount:factory:factory", e4)
        self.assertLess(TEXT.index("### E1."), TEXT.index("### G1."))
        self.assertIn("slice 16's Role", step_field("G1", "Needs") + PART_TEXT["G"])
        self.assertNotIn("matrix-launcher", "\n".join(fenced_blocks("E4")))
        self.assertNotIn("m.fixtures", e4)
        launcher_all = checks.LAUNCHER_CAN
        self.assertGreater(len([row for row in launcher_all if row[3] == "yes"]), 1)  # the verbs a `create`-only Role would fail

    def test_the_enabling_flip_is_planned_with_the_deployment_allowed_and_no_other_step_allows_it(self):
        for step, body in STEP_TEXT.items():
            fenced = "\n".join(re.findall(r"```(?:sh)?\n(.*?)```", body, re.DOTALL))
            self.assertEqual("--allow-deployment" in fenced, step == "E1", step)
        self.assertEqual(reports.parse_arguments(["plan-log", "x", "--allow-deployment"]).allow_deployment, True)
        e1 = STEP_TEXT["E1"]
        for needle in ("kubernetes_manifest.factory_deployment", "factory-relay-svc", "`factory-launcher`", "`volume-perms`", "rolls the manager"):
            self.assertIn(needle, e1)
        self.assertNotIn("(no Deployment action)", e1)


CLAIM = re.compile(r"^\| (U\d{3}) \| (.+?) \| `([^`]+)` \"(.+)\" \| (.+?) \| (.+?) \|$")


def claim_rows():
    return [m.groups() for m in (CLAIM.match(line) for line in TEXT.splitlines()) if m]


LISTED_BLOCK = re.compile(r"^\s*(?:[*-]|\d+\.)\s")


def blocks(text):
    """The blocks of a document: paragraphs, and every list item as its own block, whitespace normalised."""
    out, current = [], []
    for line in text.splitlines():
        if not line.strip():
            if current:
                out.append(current)
                current = []
            continue
        if LISTED_BLOCK.match(line) and current:
            out.append(current)
            current = []
        current.append(line)
    if current:
        out.append(current)
    return [normalise(" ".join(block)) for block in out]


# The paragraph, bullet or item of a document that says UNVERIFIED (or NOT MEASURED) and that the table covers, by file. A block that
# is exempt says why; the exemption must still match a block, so a reworded paragraph is noticed.
EXEMPT = {
    "docs/pod-backend.md": {
        "Wording used here and in every pull request": "the definition of the word, not a claim",
        "## UNVERIFIED": "a heading",
        "### Checks and UNVERIFIED": "a heading",
        "* **The table.** The runbook ends with a table": "describes the table itself, not a claim",
    },
    "docs/capacity.md": {"| Phase | Repository | Sessions": "the table of the figures the paragraph above it names"},
    "infra/README.md": {},
}


def required_blocks(path, text):
    found = blocks(text)
    if path == "docs/pod-backend.md":
        start = text.index("\n## UNVERIFIED\n")
        section = blocks(text[start : text.index("\n## ", start + 5)])
        return [b for b in found if "UNVERIFIED" in b] + [b for b in section if "UNVERIFIED" not in b]
    if path == "docs/capacity.md":
        return [b for b in found if re.search(r"UNVERIFIED|NOT MEASURED", b)]
    if path == "infra/README.md":
        return [b for b in found if re.match(r"3[6-9]\. ", b) or b.startswith("**The pod backend needs the role updated")]
    raise AssertionError(path)


class Table(unittest.TestCase):
    def test_the_table_has_rows_with_ordered_ids(self):
        rows = claim_rows()
        self.assertGreater(len(rows), 100)
        self.assertEqual([r[0] for r in rows], [f"U{n:03d}" for n in range(1, len(rows) + 1)])

    def test_every_step_a_row_names_exists_and_a_row_without_a_step_says_why(self):
        for ident, claim, path, phrase, step, kind in claim_rows():
            self.assertTrue(claim.strip(), ident)
            if step.startswith("none"):
                self.assertRegex(step, r"^none: .{12,}$", ident)
                continue
            for name in step.split(", "):
                self.assertIn(name, STEP_TEXT, f"{ident} names a step that does not exist: {name}")

    def test_every_recording_kind_a_row_names_is_in_the_vocabulary(self):
        for ident, claim, path, phrase, step, kind in claim_rows():
            for name in kind.split(", "):
                self.assertTrue(name == "-" or name in recording.KINDS, f"{ident}: {name}")

    def test_the_kind_a_row_names_is_one_the_settling_step_records(self):
        for ident, claim, path, phrase, step, kind in claim_rows():
            if kind == "-" or step.startswith("none"):
                continue
            recorded = set()
            for name in step.split(", "):
                body = STEP_TEXT[name]
                recorded |= set(re.findall(r"`([a-z][a-z-]*)`", re.search(r"^\* \*\*Record:\*\*(.*)$", body, re.MULTILINE).group(1)))
            self.assertIn(kind, recorded, f"{ident}: step {step} does not record {kind}")

    def test_every_phrase_is_still_in_the_file_it_cites(self):
        checked = 0
        for ident, claim, path, phrase, step, kind in claim_rows():
            self.assertTrue((ROOT / path).is_file(), f"{ident}: {path}")
            self.assertIn(phrase, normalise((ROOT / path).read_text(encoding="utf-8")), f"{ident} cites a phrase that is not in {path}")
            checked += 1
        self.assertGreater(checked, 90)

    def test_no_unverified_paragraph_bullet_or_item_is_missing_from_the_table(self):
        rows = claim_rows()
        covered_files = {"docs/pod-backend.md", "docs/capacity.md", "infra/README.md"}
        for path in sorted(covered_files):
            text = (ROOT / path).read_text(encoding="utf-8")
            needed = required_blocks(path, text)
            self.assertGreater(len(needed), 3, path)
            exempt = EXEMPT[path]
            for prefix in exempt:
                self.assertTrue(any(b.startswith(prefix) for b in needed + blocks(text)), f"the exemption {prefix!r} matches nothing in {path}")
            cited = [normalise(r[3]) for r in rows if r[2] == path]
            for block in needed:
                if any(block.startswith(prefix) for prefix in exempt):
                    continue
                self.assertTrue(any(phrase in block for phrase in cited), f"{path} has an UNVERIFIED block that no row covers: {block[:110]}")

    def test_the_table_covers_the_three_documents(self):
        paths = {r[2] for r in claim_rows()}
        self.assertEqual(paths, {"docs/pod-backend.md", "docs/capacity.md", "infra/README.md"})

    def test_the_hard_questions_of_the_owner_have_the_steps_that_settle_them(self):
        steps_of = {}
        for ident, claim, path, phrase, step, kind in claim_rows():
            steps_of.setdefault(normalise(claim), step)
        wanted = {
            "the Azure authorizer maps the two admission kinds to customresources/read": "F3",
            "the data-action names and the plan role's coverage of the two admission kinds": "F3",
            "the provider path accepts the admissionregistration kinds": "E1",
            "the differences between the pod's git answers and in-process mode (slice 9a bullet)": "G4",
        }
        for claim, step in wanted.items():
            self.assertEqual(steps_of[claim], step, claim)

    def test_a_claim_that_no_step_settles_is_not_one_the_runbook_could_settle_by_a_command(self):
        for ident, claim, path, phrase, step, kind in claim_rows():
            if step.startswith("none"):
                self.assertNotRegex(step, r"(?i)\b(?:run|measure|check) it\b", ident)


class Documents(unittest.TestCase):
    def test_the_program_table_marks_slice_21_as_delivered_and_nothing_run(self):
        text = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| 21 |"))
        self.assertIn("delivered", row)
        self.assertIn("nothing was run", row)
        self.assertIn("docs/pod-backend-runbook.md", row)
        self.assertIn("144 to 147", row)

    def test_the_design_record_has_a_section_for_the_slice_and_its_unverified_list(self):
        text = (ROOT / "docs" / "pod-backend.md").read_text(encoding="utf-8")
        self.assertIn("## The live runbook and its check scripts (slice 21)", text)
        section = text[text.index("## The live runbook and its check scripts (slice 21)") :]
        for needle in ("tools/live_checks.py", "tools/live_reports.py", "tools/live_recording.py", "tools/live_pod_probe.py", "UNVERIFIED (slice 21)", "hard gate", "recording format"):
            self.assertIn(needle, section)

    def test_the_architecture_page_lists_the_runbook_and_its_tools(self):
        page = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        for needle in ("docs/pod-backend-runbook.md", "live_checks.py", "live_recording.py", "live_pod_probe.py", "live_reports.py"):
            self.assertIn(needle, page)

    def test_the_infra_readme_no_longer_says_the_runbook_does_not_exist(self):
        text = (ROOT / "infra" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("and does not exist yet", text)
        self.assertIn("docs/pod-backend-runbook.md", text)

    def test_the_tools_cite_the_runbook_and_every_tool_has_a_module_docstring_naming_its_commands(self):
        for name in ("live_checks", "live_reports", "live_recording", "live_pod_probe"):
            source = (ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn("pod-backend-runbook", source if name != "live_recording" else source + "pod-backend-runbook")
            self.assertTrue(source.startswith("#!/usr/bin/env python3\n\"\"\""))


if __name__ == "__main__":
    unittest.main()
