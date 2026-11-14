"""The Terraform worker pod contract must equal the Python one (slice 0 of the pod backend).

`locals.tf` holds one local, `worker_contract`, made of literals only; `src/factory/podcontract.py`
holds the same tree as `SHARED`. This test parses the local (it does not evaluate HCL) and fails on
any value that differs and on any key or list entry that exists on one side only, so the two tracks
(Terraform and the launcher, relay and worker wrapper) cannot drift. See docs/pod-backend.md.

It runs in CI as `python3 -I -m unittest discover -s infra/test` with no package installed, so it
never imports `factory`: it loads podcontract.py by path (standard library only, no relative import).

Run from the repository root, either as discovery or by dotted module name:
    python3 -I -m unittest discover -s infra/test -v
    python3 -m unittest infra.test.test_pod_contract -v
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

# Sibling modules are imported by bare name: put this directory on sys.path so the import also works when the
# module is loaded by dotted name from the repository root (python3 -m unittest infra.test.<module>).
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_refs

INFRA = Path(__file__).resolve().parent.parent
ROOT = INFRA.parent
LOCAL = "worker_contract"
ANNOTATION = "# tflint-ignore: terraform_unused_declarations"


def load_podcontract():
    spec = importlib.util.spec_from_file_location("podcontract", ROOT / "src" / "factory" / "podcontract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotALiteral(ValueError):
    """The text uses something other than strings, integers, booleans, lists and objects."""


TOKEN = re.compile(
    r"""
    (?P<space>\s+)
    |(?P<comment>(?:\#|//)[^\n]*|/\*.*?\*/)
    |(?P<string>"(?:[^"\\\n]|\\.)*")
    |(?P<number>-?[0-9]+(?![\w.]))
    |(?P<word>[A-Za-z_][A-Za-z0-9_-]*(?![\w.(]))
    |(?P<punct>[{}\[\]=,:])
    """,
    re.VERBOSE | re.DOTALL,
)
ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


def tokens(text):
    out, position = [], 0
    while position < len(text):
        found = TOKEN.match(text, position)
        if found is None:
            raise NotALiteral(f"not a literal at line {text.count(chr(10), 0, position) + 1}")
        position = found.end()
        if found.lastgroup not in ("space", "comment"):
            out.append((found.lastgroup, found.group()))
    return out


def unquote(text):
    body = text[1:-1]
    if "${" in body or "%{" in body:
        raise NotALiteral("a template sequence in a string")
    out, index = [], 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            escape = body[index + 1] if index + 1 < len(body) else ""
            if escape not in ESCAPES:
                raise NotALiteral("an escape sequence this test does not read")
            out.append(ESCAPES[escape])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def parse_value(toks, index):
    if index >= len(toks):
        raise NotALiteral("the text ends inside a value")
    kind, text = toks[index]
    if kind == "string":
        return unquote(text), index + 1
    if kind == "number":
        return int(text), index + 1
    if kind == "word" and text in ("true", "false"):
        return text == "true", index + 1
    if (kind, text) == ("punct", "["):
        values, index = [], index + 1
        while True:
            if index >= len(toks):
                raise NotALiteral("a list is not closed")
            if toks[index] == ("punct", "]"):
                return values, index + 1
            value, index = parse_value(toks, index)
            values.append(value)
            if index < len(toks) and toks[index] == ("punct", ","):
                index += 1
    if (kind, text) == ("punct", "{"):
        values, index = {}, index + 1
        while True:
            if index >= len(toks):
                raise NotALiteral("an object is not closed")
            if toks[index] == ("punct", "}"):
                return values, index + 1
            key_kind, key = toks[index]
            if key_kind not in ("word", "string"):
                raise NotALiteral("an object key that is neither a name nor a string")
            key = unquote(key) if key_kind == "string" else key
            if index + 1 >= len(toks) or toks[index + 1] not in (("punct", "="), ("punct", ":")):
                raise NotALiteral("an object key without a value")
            if key in values:
                raise NotALiteral("a key is set twice")
            values[key], index = parse_value(toks, index + 2)
            if index < len(toks) and toks[index] == ("punct", ","):
                index += 1
    raise NotALiteral("not a string, integer, boolean, list or object")


def parse_literal(text):
    toks = tokens(text)
    value, index = parse_value(toks, 0)
    if index != len(toks):
        raise NotALiteral("text after the value")
    return value


def differences(python, terraform, path=LOCAL):
    """Every place where the two trees differ, in words. Empty when they are equal."""
    if isinstance(python, dict) and isinstance(terraform, dict):
        out = []
        for key in sorted(python.keys() | terraform.keys()):
            if key not in terraform:
                out.append(f"{path}.{key}: only in podcontract.SHARED")
            elif key not in python:
                out.append(f"{path}.{key}: only in infra/locals.tf")
            else:
                out.extend(differences(python[key], terraform[key], f"{path}.{key}"))
        return out
    if isinstance(python, list) and isinstance(terraform, list):
        if len(python) != len(terraform):
            return [f"{path}: {len(python)} entries in podcontract.SHARED, {len(terraform)} in infra/locals.tf"]
        out = []
        for index, (left, right) in enumerate(zip(python, terraform)):
            out.extend(differences(left, right, f"{path}[{index}]"))
        return out
    if type(python) is not type(terraform) or python != terraform:
        return [f"{path}: podcontract.SHARED has {python!r}, infra/locals.tf has {terraform!r}"]
    return []


def locals_text():
    return (INFRA / "locals.tf").read_text(encoding="utf-8")


def contract_literal():
    """The text of the `{...}` value of the worker_contract local, comments included."""
    text = locals_text()
    view = check_refs.blank_view(text)
    found = list(re.finditer(rf"^[ \t]*{LOCAL}[ \t]*=[ \t]*\{{", view, re.MULTILINE))
    if len(found) != 1:
        raise AssertionError(f"expected exactly one `{LOCAL} = {{` in locals.tf, found {len(found)}")
    opening = found[0].end() - 1
    return text[opening : check_refs.match_brace(view, opening)]


def local_value(name):
    """The string or integer a simple `name = value` local of locals.tf is set to."""
    found = re.search(rf'^[ \t]*{re.escape(name)}[ \t]*=[ \t]*("[^"\n]*"|[0-9]+)[ \t]*$', locals_text(), re.MULTILINE)
    if found is None:
        raise AssertionError(f"locals.tf has no simple local {name}")
    raw = found.group(1)
    return raw[1:-1] if raw.startswith('"') else int(raw)


def tf_files():
    return sorted(INFRA.glob("*.tf"))


class ContractMatchesPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pc = load_podcontract()
        cls.terraform = parse_literal(contract_literal())

    def test_terraform_and_python_hold_the_same_contract(self):
        self.assertEqual(differences(self.pc.SHARED, self.terraform), [])

    def test_the_terraform_contract_has_the_documented_sections(self):
        self.assertEqual(
            sorted(self.terraform),
            [
                "annotations",
                "canary",
                "classes",
                "env",
                "env_values",
                "ids",
                "labels",
                "limits",
                "names",
                "nodes",
                "paths",
                "ports",
                "timing",
                "volumes",
            ],
        )

    def test_the_values_terraform_already_had_are_the_ones_the_contract_repeats(self):
        names, ports = self.terraform["names"], self.terraform["ports"]
        self.assertEqual(names["manager_namespace"], local_value("k8s_namespace"))
        self.assertEqual(ports["app"], local_value("app_port"))
        self.assertEqual(ports["metrics"], local_value("metrics_port"))

    def test_the_spool_refusal_threshold_is_at_most_75_percent_of_the_spool_size_limit(self):
        limit = self.pc.parse_quantity(self.terraform["volumes"]["spool_size_limit"])
        self.assertGreater(self.pc.SPOOL_REFUSE_BYTES, 0)
        self.assertLessEqual(self.pc.SPOOL_REFUSE_BYTES * 4, limit * 3)

    def test_no_python_only_constant_is_set_or_mentioned_by_terraform(self):
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                keys.update(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.terraform)
        self.assertTrue(self.pc.PYTHON_ONLY)
        for name in self.pc.PYTHON_ONLY:
            with self.subTest(name=name):
                self.assertNotIn(name.lower(), keys)
                for path in tf_files():
                    text = path.read_text(encoding="utf-8")
                    self.assertIsNone(re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE), path.name)
                    value = getattr(self.pc, name)
                    if isinstance(value, str):
                        self.assertNotIn(value, text, path.name)


class TheLocalIsLiteralsOnly(unittest.TestCase):
    def test_the_local_parses_without_evaluating_anything(self):
        value = parse_literal(contract_literal())
        self.assertIsInstance(value, dict)
        self.assertTrue(value)

    def test_the_literal_has_no_reference_function_or_template(self):
        text = check_refs.blank_view(contract_literal())
        self.assertIsNone(check_refs.VAR_REF.search(text))
        self.assertIsNone(check_refs.LOCAL_REF.search(text))
        self.assertIsNone(check_refs.DATA_REF.search(text))
        self.assertIsNone(check_refs.RESOURCE_REF.search(text))
        self.assertIsNone(re.search(r"\w\(", text))
        self.assertNotIn("${", contract_literal())
        self.assertNotIn("%{", contract_literal())

    def test_the_parser_reads_what_the_local_uses(self):
        text = """{
          # a comment
          one = "a"   // another
          two = 2, three = true
          "quoted-key" = ["x", "y",]
          nested = { list = [ { a = 1, b = "c" }, { a = 2 } ] }
        }"""
        self.assertEqual(
            parse_literal(text),
            {
                "one": "a",
                "two": 2,
                "three": True,
                "quoted-key": ["x", "y"],
                "nested": {"list": [{"a": 1, "b": "c"}, {"a": 2}]},
            },
        )
        self.assertEqual(parse_literal('"a\\"b"'), 'a"b')

    def test_the_parser_refuses_everything_that_is_not_a_literal(self):
        for bad in (
            "{ a = var.x }",
            "{ a = local.y }",
            "{ a = data.azurerm_x.y.id }",
            'x = { a = "${var.x}" }',
            '{ a = "${local.x}" }',
            '{ a = "%{ if true }a%{ endif }" }',
            "{ a = max(1, 2) }",
            "{ a = 1.5 }",
            "{ a = null }",
            "{ a = [1, 2 }",
            "{ a = 1",
            "{ a = 1 } extra",
            "{ a = 1, a = 2 }",
            "{ a }",
            "{ a = b }",
            '{ a = "x" == "y" }',
            "{ a = cond ? 1 : 2 }",
            '{ a = "\\u0041" }',
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(NotALiteral):
                parse_literal(bad)

    def test_the_comparison_names_a_changed_value_a_missing_key_an_extra_key_and_a_type_change(self):
        python = {"a": 1, "b": {"c": "x", "d": [1, 2]}, "e": True}
        self.assertEqual(differences(python, {"a": 1, "b": {"c": "x", "d": [1, 2]}, "e": True}), [])
        self.assertEqual(
            differences(python, {"a": 2, "b": {"c": "x", "d": [1, 2]}, "e": True}),
            [f"{LOCAL}.a: podcontract.SHARED has 1, infra/locals.tf has 2"],
        )
        self.assertEqual(
            differences(python, {"a": 1, "b": {"d": [1, 2]}, "e": True}),
            [f"{LOCAL}.b.c: only in podcontract.SHARED"],
        )
        self.assertEqual(
            differences(python, {"a": 1, "b": {"c": "x", "d": [1, 2]}, "e": True, "f": 1}),
            [f"{LOCAL}.f: only in infra/locals.tf"],
        )
        self.assertEqual(
            differences(python, {"a": 1, "b": {"c": "x", "d": [1]}, "e": True}),
            [f"{LOCAL}.b.d: 2 entries in podcontract.SHARED, 1 in infra/locals.tf"],
        )
        self.assertEqual(
            differences(python, {"a": 1, "b": {"c": "x", "d": [1, 3]}, "e": True}),
            [f"{LOCAL}.b.d[1]: podcontract.SHARED has 2, infra/locals.tf has 3"],
        )
        # True == 1 in Python: a boolean must not stand in for an integer.
        self.assertEqual(len(differences({"a": 1}, {"a": True})), 1)
        self.assertEqual(len(differences({"a": "1"}, {"a": 1})), 1)


class TheAnnotationLivesExactlyAsLongAsNothingReadsTheLocal(unittest.TestCase):
    """tflint's recommended preset fails an unused local, so the local carries an ignore annotation.

    The first Terraform slice that references local.worker_contract must remove the annotation, and
    this test fails when it does not (a stale annotation) and when the annotation is missing or not
    on the line just above the local while nothing reads it (tflint would fail the job).
    """

    def read_anywhere(self):
        for path in tf_files():
            view = check_refs.blank_view(path.read_text(encoding="utf-8"))
            if LOCAL in check_refs.LOCAL_REF.findall(view):
                return True
        return False

    def annotated(self):
        lines = locals_text().splitlines()
        indexes = [i for i, line in enumerate(lines) if re.match(rf"\s*{LOCAL}\s*=\s*\{{", line)]
        self.assertEqual(len(indexes), 1)
        return indexes[0] > 0 and lines[indexes[0] - 1].strip() == ANNOTATION

    def test_the_annotation_is_there_while_nothing_reads_the_local_and_gone_when_something_does(self):
        if self.read_anywhere():
            self.assertFalse(self.annotated(), "remove the tflint-ignore annotation: the local is read now")
        else:
            self.assertTrue(
                self.annotated(),
                "the local is unused: put the tflint-ignore annotation on the line just above it",
            )

    def test_no_other_rule_is_silenced_for_the_local(self):
        # While nothing reads the local exactly one annotation exists; once something reads it (slice 14: worker_pool.tf) none may.
        text = locals_text()
        self.assertEqual(text.count("tflint-ignore"), 0 if self.read_anywhere() else 1)


if __name__ == "__main__":
    unittest.main()
