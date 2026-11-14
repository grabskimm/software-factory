"""scripts/tfvars_repo_configs.py: finding and editing the repo_configs map of an INFRA_TFVARS text."""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "tfvars_repo_configs.py"
EXAMPLE = ROOT / "infra" / "terraform.tfvars.example"

spec = importlib.util.spec_from_file_location("tfvars_repo_configs", HELPER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

KEY = "acme/newrepo"
ENTRY = '''repo            = "acme/newrepo"
base_branch     = "main"
installation_id = 111

[worker]
uid = 10002
gid = 10010

[sandbox]
verify_domains = ["pypi.org"]

[preflight]
probe_url = "https://pypi.org/"
'''


def plan(text, key=KEY, entry=ENTRY, replace=False, gh_newline=True, ignore=()):
    """gh prints the value plus one newline; the helper takes it off again."""
    return mod.plan(text + ("\n" if gh_newline else ""), key, entry, replace, ignore)


def block(indent="  ", eol="\n", entry=ENTRY):
    lines = ['%s"%s" = <<-TOML' % (indent, KEY)] + [(indent + "  " + x) if x else "" for x in entry.rstrip("\n").split("\n")] + [indent + "TOML"]
    return eol.join(lines) + eol


class Inserts(unittest.TestCase):
    def check_insert(self, text, expected_at=None):
        status, diff, new = plan(text)
        self.assertEqual(status, "inserted")
        added = block()
        start = new.index('"acme/newrepo" = <<-TOML') if expected_at is None else expected_at
        self.assertIn(added.strip(), new)
        # everything but the inserted lines is byte for byte the original
        again, _, _ = plan(new)
        self.assertEqual(again, "unchanged")
        return new, diff, start

    def test_the_example_file(self):
        text = EXAMPLE.read_text()
        status, diff, new = mod.plan(text + "\n", KEY, ENTRY, False)
        self.assertEqual(status, "inserted")
        expected = text.replace("  TOML\n}\n", "  TOML\n" + block() + "}\n", 1)
        self.assertEqual(new, expected)
        self.assertIn('+  "acme/newrepo" = <<-TOML', diff)
        self.assertIn("+    probe_url", diff)
        # only the repo_configs region is in the diff
        for other in ("subscription_id", "aks_sku_tier", "acr_name", "entra_allowed_audiences"):
            self.assertNotIn(other, diff)
        self.assertLessEqual(diff.count("\n"), 40)

    def test_the_example_file_with_crlf(self):
        text = EXAMPLE.read_text().replace("\n", "\r\n")
        status, diff, new = mod.plan(text + "\n", KEY, ENTRY, False)
        self.assertEqual(status, "inserted")
        self.assertEqual(new, text.replace("  TOML\r\n}\r\n", "  TOML\r\n" + block(eol="\r\n") + "}\r\n", 1))
        self.assertEqual(new.count("\n"), new.count("\r\n"))
        self.assertNotIn("\r", diff)
        self.assertEqual(mod.plan(new + "\n", KEY, ENTRY, False)[0], "unchanged")

    def test_lf_file_with_a_stray_crlf_stays_lf(self):
        text = "repo_configs = {\n}\nx = 1\r\ny = 2\n"
        status, _, new = plan(text)
        self.assertEqual(status, "inserted")
        self.assertEqual(new.count("\r\n"), 1)

    def test_empty_map_on_one_line(self):
        status, diff, new = plan('a = 1\nrepo_configs = {}\nb = 2\n')
        self.assertEqual(status, "inserted")
        self.assertTrue(new.startswith("a = 1\nrepo_configs = {\n"))
        self.assertTrue(new.endswith('  TOML\n}\nb = 2\n'))
        self.assertEqual(plan(new)[0], "unchanged")

    def test_empty_map_over_lines_and_indented_assignment(self):
        status, _, new = plan("  repo_configs = {\n  }\n")
        self.assertEqual(status, "inserted")
        self.assertIn('\n    "acme/newrepo" = <<-TOML\n', new)
        self.assertTrue(new.endswith("\n    TOML\n  }\n"))

    def test_single_line_map_with_a_string_entry(self):
        status, _, new = plan('repo_configs = { "a/b" = "x = 1" }\nmore = 2\n')
        self.assertEqual(status, "inserted")
        self.assertTrue(new.startswith('repo_configs = { "a/b" = "x = 1" \n'))
        self.assertTrue(new.endswith('  TOML\n}\nmore = 2\n'))
        self.assertEqual(plan(new)[0], "unchanged")

    def test_comments_blank_lines_and_other_keys_on_both_sides(self):
        text = '''# header
// another comment style
/* block
   comment with repo_configs = { and } inside */
before = "}"

# repo_configs = { "commented/out" = "x" }
repo_configs = { # trailing comment {
  # comment inside the map
  "acme/one" = <<-EOT
    repo = "acme/one"
    note = "braces { } and the word TOML and repo_configs = {"
  EOT

  // another one
  "acme/two" = <<TOML
repo = "acme/two"
 TOML
}   # after the map

after = {
  repo_configs = "not top level"
}
'''
        status, diff, new = plan(text)
        self.assertEqual(status, "inserted")
        at = new.index('  "acme/newrepo"')
        self.assertEqual(new[:at] + new[at + len(block()):], text)
        self.assertTrue(new[at + len(block()):].startswith("}   # after the map"))
        self.assertEqual(plan(new)[0], "unchanged")

    def test_tabs_and_deep_indentation_follow_the_existing_entries(self):
        text = 'repo_configs = {\n\t\t"a/b" = <<-TOML\n\t\t\tx = 1\n\t\tTOML\n}\n'
        status, _, new = plan(text)
        self.assertEqual(status, "inserted")
        self.assertIn('\n\t\t"acme/newrepo" = <<-TOML\n\t\t  repo ', new)

    def test_other_top_level_keys_with_strings_braces_and_heredocs(self):
        text = '''locals_like = "x ${var.a} {{ }}"
url = "https://example.invalid//path" # not a comment start inside the string
other = <<EOF
repo_configs = {
  "fake/entry" = "x"
}
EOF
tags = { a = "}" }
list = [ "a", { b = 1 } ]
repo_configs = {
  "acme/one" = "text with } and # and //"
}
tail = "${"}"}"
'''
        status, _, new = plan(text)
        self.assertEqual(status, "inserted")
        self.assertIn('  "acme/one" = "text with } and # and //"\n  "acme/newrepo" = <<-TOML', new)
        self.assertEqual(plan(new)[0], "unchanged")

    def test_value_without_trailing_newline_and_with_bom(self):
        status, _, new = plan("repo_configs = {\n}", gh_newline=True)
        self.assertEqual(status, "inserted")
        self.assertTrue(new.endswith("TOML\n}"))
        status, _, new = plan("﻿repo_configs = {\n}\n")
        self.assertEqual(status, "inserted")

    def test_non_ascii_text_survives_byte_for_byte(self):
        text = '# Zeichen: äöü €\nrepo_configs = {\n}\n'
        status, _, new = plan(text)
        self.assertEqual(status, "inserted")
        self.assertTrue(new.startswith('# Zeichen: äöü €\n'))

    def test_dollar_and_percent_escapes_in_strings(self):
        text = 'a = "$${not_interpolated} %%{also_not}"\nrepo_configs = {\n}\n'
        self.assertEqual(plan(text)[0], "inserted")

    def test_fresh_entry_is_inserted_last_so_existing_order_is_kept(self):
        text = 'repo_configs = {\n  "z/z" = "1"\n  "a/a" = "2"\n}\n'
        _, _, new = plan(text)
        self.assertLess(new.index('"z/z"'), new.index('"a/a"'))
        self.assertLess(new.index('"a/a"'), new.index('"acme/newrepo"'))


class Existing(unittest.TestCase):
    TEXT = 'repo_configs = {\n' + block(indent="    ", entry=ENTRY) + '  "acme/other" = <<-TOML\n    repo = "acme/other"\n  TOML\n}\nlast = 1\n'

    def test_identical_even_with_other_indentation_crlf_and_trailing_blanks(self):
        for text in (self.TEXT,
                     self.TEXT.replace("\n", "\r\n"),
                     'repo_configs = {\n  "acme/newrepo" = <<TOML\n' + ENTRY + '\n\nTOML\n}\n',
                     'repo_configs = {\n  "acme/newrepo" = <<-X\n' + "\n".join("\t" + x for x in ENTRY.split("\n")) + 'X\n}\n'):
            status, diff, new = plan(text)
            self.assertEqual(status, "unchanged", text)
            self.assertEqual(diff, "")
            self.assertEqual(new, text)

    def test_different_content_shows_the_entry_only(self):
        entry = ENTRY.replace('["pypi.org"]', '["pypi.org", "files.pythonhosted.org"]')
        status, diff, new = plan(self.TEXT, entry=entry)
        self.assertEqual(status, "differs")
        self.assertEqual(new, self.TEXT)
        self.assertIn('-verify_domains = ["pypi.org"]', diff)
        self.assertIn('+verify_domains = ["pypi.org", "files.pythonhosted.org"]', diff)
        self.assertNotIn("acme/other", diff)
        self.assertNotIn("last = 1", diff)

    def test_replace_swaps_that_entry_and_nothing_else(self):
        entry = ENTRY + 'run_budget_usd = 3\n'
        status, diff, new = plan(self.TEXT, entry=entry, replace=True)
        self.assertEqual(status, "replaced")
        self.assertIn("+      run_budget_usd = 3", diff)
        old = block(indent="    ", entry=ENTRY)
        self.assertEqual(new, self.TEXT.replace(old, block(indent="    ", entry=entry), 1))
        self.assertEqual(plan(new, entry=entry)[0], "unchanged")

    def test_replace_in_crlf_text(self):
        text = self.TEXT.replace("\n", "\r\n")
        entry = ENTRY + 'run_budget_usd = 3\n'
        status, _, new = plan(text, entry=entry, replace=True)
        self.assertEqual(status, "replaced")
        self.assertEqual(new.count("\n"), new.count("\r\n"))
        self.assertTrue(new.endswith("}\r\nlast = 1\r\n"))

    def test_replace_a_plain_string_entry(self):
        text = 'repo_configs = {\n  "acme/newrepo" = "old"\n  "b/c" = "x"\n}\n'
        status, diff, new = plan(text)
        self.assertEqual(status, "differs")
        self.assertIn("-", diff)
        status, _, new = plan(text, replace=True)
        self.assertEqual(status, "replaced")
        self.assertIn('  "b/c" = "x"\n}', new)
        self.assertNotIn('= "old"', new)

    def replaced_ok(self, text):
        """Replace the entry, require valid shape: the terminator alone on its line, a stable re-plan."""
        status, diff, new = plan(text, replace=True)
        self.assertEqual(status, "replaced")
        for line in new.split("\n"):
            if line.strip().startswith("TOML"):
                self.assertEqual(line.strip(), "TOML", "the heredoc terminator must be alone on its line: %r" % line)
        self.assertEqual(plan(new)[0], "unchanged")
        self.assertEqual(mod.plan(new, KEY, ENTRY, True)[0], "unchanged")
        return new, diff

    def test_replace_when_the_key_is_not_first_on_its_line(self):
        text = 'repo_configs = { "acme/newrepo" = "old", "b/c" = "x" }\n'
        new, _ = self.replaced_ok(text)
        self.assertTrue(new.startswith('repo_configs = { "acme/newrepo" = <<-TOML\n'))
        self.assertTrue(new.endswith('TOML\n  "b/c" = "x" }\n'))
        text = 'repo_configs = { "b/c" = "x", "acme/newrepo" = "old" }\n'
        new, _ = self.replaced_ok(text)
        self.assertTrue(new.startswith('repo_configs = { "b/c" = "x", "acme/newrepo" = <<-TOML\n'))
        self.assertTrue(new.endswith('TOML\n}\n'))
        self.replaced_ok('repo_configs = { "acme/newrepo" = "old" }\n')

    def test_replace_keeps_the_text_after_the_old_value_off_the_terminator_line(self):
        # a trailing comma, a trailing comment, both, the next entry: each used to land on the TOML line
        for tail in (",", " # note", ", # note", " // note", " /* note */", ","):
            text = 'repo_configs = {\n  "acme/existing" = "x",\n  "acme/newrepo" = "old"%s\n  "z/y" = "q"\n}\nlast = 1\n' % tail
            new, _ = self.replaced_ok(text)
            self.assertIn('  TOML\n', new, tail)
            self.assertTrue(new.endswith('"z/y" = "q"\n}\nlast = 1\n'), tail)
            if "note" in tail:
                self.assertIn("note", new)
            self.assertNotIn("TOML,", new)
        new, _ = self.replaced_ok('repo_configs = {\n  "acme/newrepo" = "old",\n}\n')
        self.assertTrue(new.endswith("  TOML\n}\n"))
        new, _ = self.replaced_ok('repo_configs = {\n  "acme/newrepo" = "old" # note\n}\n')
        self.assertIn("  TOML\n  # note\n}", new)
        new, _ = self.replaced_ok('repo_configs = {\n  "acme/newrepo" = "old", "z/y" = "q"\n}\n')
        self.assertIn('  TOML\n  "z/y" = "q"\n}', new)
        crlf = 'repo_configs = {\r\n  "acme/newrepo" = "old", # n\r\n  "z/y" = "q"\r\n}\r\n'
        status, _, new = plan(crlf, replace=True)
        self.assertEqual(status, "replaced")
        self.assertEqual(new.count("\n"), new.count("\r\n"))
        self.assertNotIn("TOML,", new)

    @unittest.skipUnless(shutil.which("terraform"), "terraform not installed")
    def test_replaced_texts_are_valid_hcl(self):
        cases = ['repo_configs = {\n  "acme/existing" = "x",\n  "acme/newrepo" = "old",\n}\n',
                 'repo_configs = {\n  "acme/newrepo" = "old" # note\n}\n',
                 'repo_configs = {\n  "acme/newrepo" = "old", # note\n  "z/y" = "q"\n}\n',
                 'repo_configs = { "acme/newrepo" = "old", "z/y" = "q" }\n',
                 'repo_configs = { "z/y" = "q", "acme/newrepo" = "old" }\n',
                 'repo_configs = { "acme/newrepo" = "old" }\n']
        tmp = Path(tempfile.mkdtemp(prefix="tfvars-hcl-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for i, text in enumerate(cases):
            for replace, source in ((True, text), (False, text.replace('"acme/newrepo" = "old"', '"b/c" = "o"'))):
                new = plan(source, replace=replace)[2]
                path = tmp / ("case%d%s.tfvars" % (i, "r" if replace else "i"))
                path.write_text(new + "\n")
                proc = subprocess.run(["terraform", "fmt", "-no-color", "-write=false", "-list=false", str(path)],
                                      capture_output=True, text=True, cwd=tmp)
                self.assertEqual(proc.returncode, 0, "%r\n%s" % (new, proc.stderr))

    def test_key_in_other_case_and_duplicates_are_refused(self):
        with self.assertRaises(mod.Refused):
            plan('repo_configs = {\n  "ACME/NewRepo" = "x"\n}\n')
        with self.assertRaises(mod.Refused):
            plan('repo_configs = {\n  "acme/newrepo" = "x"\n  "acme/newrepo" = "y"\n}\n')

    def test_similar_keys_do_not_count(self):
        text = 'repo_configs = {\n  "acme/newrepo-2" = "x"\n  "other/newrepo" = "y"\n}\n'
        self.assertEqual(plan(text)[0], "inserted")


class Region(unittest.TestCase):
    def test_the_diff_stops_at_the_braces_so_a_comment_there_is_never_printed(self):
        text = ('x = 1\nrepo_configs = { # ghp_FAKE_OPEN_COMMENT\n  "acme/existing" = "x"\n} # ghp_FAKE_CLOSE_COMMENT\n'
                'y = "ghp_FAKE_OTHER"\n')
        for replace, source in ((False, text), (True, text.replace('"acme/existing"', '"acme/newrepo"'))):
            status, diff, new = plan(source, replace=replace)
            self.assertIn(status, ("inserted", "replaced"))
            self.assertNotIn("ghp_FAKE", diff)
            self.assertIn("+  ", diff)
            self.assertIn("repo_configs = {", diff)
            self.assertTrue(diff.rstrip().endswith("}") or "}" in diff.splitlines()[-1])
            self.assertIn("ghp_FAKE_CLOSE_COMMENT", new)  # the text itself keeps them
            self.assertEqual(plan(new)[0], "unchanged")

    def test_closing_brace_on_the_last_entry_line(self):
        status, diff, new = plan('repo_configs = { "a/b" = "x" } # ghp_FAKE\n')
        self.assertEqual(status, "inserted")
        self.assertNotIn("ghp_FAKE", diff)


class SelfCheck(unittest.TestCase):
    def test_a_result_that_does_not_re_parse_is_refused(self):
        real = mod.render_entry

        def broken(key, lines, indent):
            out = real(key, lines, indent)
            out[-1] = out[-1] + ","  # a terminator that is not alone on its line
            return out
        mod.render_entry = broken
        try:
            for text in ('repo_configs = {\n}\n', 'repo_configs = {\n  "acme/newrepo" = "old"\n}\n'):
                with self.assertRaises(mod.Refused) as ctx:
                    plan(text, replace=True)
                self.assertIn("internal check", str(ctx.exception))
        finally:
            mod.render_entry = real

    def test_another_entry_changing_is_refused(self):
        real = mod.render_entry

        def broken(key, lines, indent):
            return real(key, lines, indent) + ['"z/y" = "q"']  # smuggles a second entry
        mod.render_entry = broken
        try:
            with self.assertRaises(mod.Refused):
                plan('repo_configs = {\n}\n')
        finally:
            mod.render_entry = real


class IgnoredKeys(unittest.TestCase):
    TEXT = ('repo_configs = {\n  "acme/newrepo" = <<-TOML\n    repo = "acme/newrepo"\n    run_budget_usd = 20\n\n'
            '    [sandbox]\n    verify_domains = [\n      "a.example",\n      "b.example",\n    ]\n\n    [preflight]\n'
            '    probe_url = "https://a.example/"\n  TOML\n}\n')
    WANT = 'repo = "acme/newrepo"\n\n[sandbox]\nverify_domains = ["pypi.org"]\n\n[preflight]\nprobe_url = "https://pypi.org/"\n'

    def test_fields_not_asked_for_do_not_make_an_entry_differ(self):
        self.assertEqual(plan(self.TEXT, entry=self.WANT)[0], "differs")
        self.assertEqual(plan(self.TEXT, entry=self.WANT, ignore=("run_budget_usd", "verify_domains", "probe_url"))[0], "unchanged")
        status, diff, _ = plan(self.TEXT, entry=self.WANT, ignore=("run_budget_usd",))
        self.assertEqual(status, "differs")
        self.assertIn("verify_domains", diff)
        self.assertNotIn("run_budget_usd", diff)  # an ignored field is not part of the comparison either

    def test_passed_fields_still_count_and_replace_writes_the_whole_entry(self):
        want = self.WANT.replace("repo = ", 'base_branch = "dev"\nrepo = ')
        self.assertEqual(plan(self.TEXT, entry=want, ignore=("run_budget_usd", "verify_domains", "probe_url"))[0], "differs")
        status, _, new = plan(self.TEXT, entry=want, replace=True, ignore=("run_budget_usd",))
        self.assertEqual(status, "replaced")
        self.assertNotIn("run_budget_usd", new)  # replaced wholesale, as documented
        self.assertEqual(plan(new, entry=want)[0], "unchanged")  # the strict comparison holds

    def test_cli_option(self):
        tmp = Path(tempfile.mkdtemp(prefix="tfvars-ignore-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "raw").write_text(self.TEXT + "\n")
        (tmp / "entry").write_text(self.WANT)
        base = [sys.executable, str(HELPER), "plan", "--raw", str(tmp / "raw"), "--key", KEY, "--entry", str(tmp / "entry")]
        self.assertEqual(json.loads(subprocess.run(base, capture_output=True, text=True).stdout)["status"], "differs")
        proc = subprocess.run(base + ["--ignore-key", "run_budget_usd", "--ignore-key", "verify_domains", "--ignore-key", "probe_url"],
                              capture_output=True, text=True)
        self.assertEqual(json.loads(proc.stdout)["status"], "unchanged")


class Refuses(unittest.TestCase):
    def refused(self, text, **kw):
        with self.assertRaises(mod.Refused) as ctx:
            plan(text, **kw)
        return str(ctx.exception)

    def test_no_map(self):
        self.refused("")
        self.refused('subscription_id = "x"\n')
        self.refused('# repo_configs = {}\n')
        self.refused('x = "repo_configs = {}"\n')
        self.refused('tags = {\n  repo_configs = {}\n}\n')
        self.refused('other = <<EOT\nrepo_configs = {}\nEOT\n')
        self.refused('repo_configs {\n}\n')

    def test_more_than_one_map(self):
        msg = self.refused('repo_configs = {}\nrepo_configs = {}\n')
        self.assertIn("exactly one", msg)

    def test_not_a_literal_map(self):
        self.assertIn("literal", self.refused('repo_configs = merge(local.a, { "x/y" = "z" })\n'))
        self.refused('repo_configs = var.configs\n')
        self.refused('repo_configs = [ "a" ]\n')

    def test_text_that_cannot_be_read_with_certainty(self):
        self.refused('repo_configs = {\n  "a/b" = <<-TOML\n  x = 1\n}\n')  # unterminated heredoc
        self.refused('repo_configs = {\n  "a/b" = "unterminated\n}\n')
        self.refused('repo_configs = {\n')
        self.refused('repo_configs = {\n}\n}\n')
        self.refused('repo_configs = {\n  "a/b" = "x"\n/* open comment\n}\n')
        self.refused('repo_configs = {\n  "a/b" "x"\n}\n')
        self.refused('repo_configs = {\n  "a/b" = "${unclosed"\n}\n')
        self.refused('repo_configs = {\n  "a/b" = "x" + "y"\n}\n')

    def test_unsafe_keys_and_entries(self):
        for key in ('a"b/c', "a/b c", "a/b\n", "a/../b", "a", "a/b/c", "/b", "a/", "$(x)/y", "a/${x}"):
            self.refused('repo_configs = {\n}\n', key=key)
        for entry in ("", "\n\n", "x = 1\nTOML\ny = 2\n", 'x = "${var.a}"\n', 'x = "%{if a}"\n', "x = 1\x00\n"):
            self.refused('repo_configs = {\n}\n', entry=entry)

    def test_messages_never_quote_the_text(self):
        secretish = 'the_secret_marker = "S3CR3T-VALUE"\n'
        for text in (secretish, secretish + 'repo_configs = {\n', secretish + 'repo_configs = merge(a)\n',
                     secretish + 'repo_configs = {\n"a/b" = "unterminated\n}\n'):
            self.assertNotIn("S3CR3T", self.refused(text))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tfvars-helper-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.entry = self.tmp / "entry"
        self.entry.write_text(ENTRY)
        self.raw = self.tmp / "raw"

    def run_helper(self, text, *extra):
        self.raw.write_bytes(text.encode() if isinstance(text, str) else text)
        out = self.tmp / "out"
        proc = subprocess.run([sys.executable, str(HELPER), "plan", "--raw", str(self.raw), "--key", KEY, "--entry", str(self.entry),
                               "--out", str(out), *extra], capture_output=True, text=True)
        return proc, out

    def test_json_output_and_out_file(self):
        proc, out = self.run_helper("keep = 1\nrepo_configs = {\n}\n\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["status"], "inserted")
        self.assertEqual(data["size"], len(out.read_bytes()))
        self.assertTrue(out.read_text().startswith("keep = 1\nrepo_configs = {\n  "))
        self.assertTrue(out.read_text().endswith("}\n"))  # gh's one newline removed, the value's own kept
        self.assertEqual(proc.stderr, "")

    def test_unchanged_and_differs_write_nothing(self):
        proc, out = self.run_helper("repo_configs = {\n}\n")
        text = out.read_text()
        out.unlink()
        proc, out = self.run_helper(text + "\n")
        self.assertEqual(json.loads(proc.stdout)["status"], "unchanged")
        self.assertFalse(out.exists())
        self.entry.write_text(ENTRY + "x = 1\n")
        proc, out = self.run_helper(text + "\n")
        self.assertEqual(json.loads(proc.stdout)["status"], "differs")
        self.assertFalse(out.exists())
        proc, out = self.run_helper(text + "\n", "--replace")
        self.assertEqual(json.loads(proc.stdout)["status"], "replaced")
        self.assertTrue(out.exists())

    def test_refusal_exit_code_and_empty_stdout(self):
        proc, out = self.run_helper("nothing here\n")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, "")
        self.assertIn("refused:", proc.stderr)
        self.assertNotIn("nothing here", proc.stderr)
        self.assertFalse(out.exists())

    def test_usage_and_missing_files(self):
        proc = subprocess.run([sys.executable, str(HELPER), "plan"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        proc = subprocess.run([sys.executable, str(HELPER), "plan", "--raw", "/nonexistent/x", "--key", KEY, "--entry", str(self.entry)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)

    def test_invalid_utf8_passes_through_unchanged(self):
        raw = b'# \xff\xfe odd bytes\nrepo_configs = {\n}\n\n'
        proc, out = self.run_helper(raw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.read_bytes().startswith(b'# \xff\xfe odd bytes\nrepo_configs = {\n'))

    def test_main_function_in_process(self):
        self.raw.write_text("repo_configs = {\n}\n\n")
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = mod.main(["plan", "--raw", str(self.raw), "--key", KEY, "--entry", str(self.entry)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "inserted")


if __name__ == "__main__":
    unittest.main()
