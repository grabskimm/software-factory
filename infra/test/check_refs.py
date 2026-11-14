#!/usr/bin/env python3
"""Offline consistency check for the Terraform in infra/ (standard library only).

`terraform validate` already rejects references to undeclared objects, but it needs
provider plugins and says nothing about a variable that nobody reads. This script needs
neither: it reads the *.tf files with a small lexer (comments and string text are blanked,
interpolations are kept) and fails when

  * a variable is declared but never used (a reference inside the variable's own
    validation block does not count as a use);
  * a variable, local, resource or data reference points at nothing, with outputs named
    in the message because the operator relies on them;
  * a GUID-shaped literal appears in a .tf file, the example variable file or the README
    (the all-zero GUID is allowed, and so is each entry of PUBLIC_CONSTANT_GUIDS in the one
    file named there), or a long hex literal that looks like a digest or a Key Vault secret
    version appears in a .tf file;
  * Terraform manages a secret value: azurerm_key_vault_secret (data or resource), the ARM
    type Microsoft.KeyVault/vaults/secrets, a kubernetes_secret / kubernetes_secret_v1
    resource or data source (including the ephemeral and write-only variants), or a
    kubernetes_manifest of kind Secret;
  * terraform.tfvars.example plus image.auto.tfvars do not assign exactly the variables
    that have no default (a missing one or an unknown one is reported).

Usage: check_refs.py [DIRECTORY]   (default: the infra/ directory next to this file)
Exit status 0 when clean, 1 when a problem is found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])")
NIL_GUID = "00000000-0000-0000-0000-000000000000"
# Public, tenant-independent application IDs that must appear as literals. Each entry is
# allowed in exactly one .tf file and nowhere else (not in the example or the README, and not
# in another .tf file), so an operator-specific GUID cannot hide behind it.
PUBLIC_CONSTANT_GUIDS = {
    # "Azure Kubernetes Service AAD Server": the token audience kubelogin needs (--server-id).
    "6dae42f8-4368-4678-94ff-3960e28e3630": "locals.tf",
}
LONG_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
BLOCK = re.compile(
    r'^(variable|output|resource|data|ephemeral|provider|locals|terraform|module)\b[ \t]*((?:"[ ]*"[ \t]*){0,2})\{',
    re.M,
)
VAR_REF = re.compile(r"(?<![\w.])var\.([A-Za-z_][\w-]*)")
LOCAL_REF = re.compile(r"(?<![\w.])local\.([A-Za-z_][\w-]*)")
DATA_REF = re.compile(r"(?<![\w.])data\.([A-Za-z][\w]*)\.([A-Za-z_][\w-]*)")
RESOURCE_REF = re.compile(r"(?<![\w.])((?:azurerm|azapi|kubernetes)_[a-z0-9_]+)\.([A-Za-z_][\w-]*)")
SECRET_KINDS = re.compile(r"^(?:azurerm_key_vault_secret|kubernetes_secret(?:_v1)?(?:_[a-z0-9_]+)?)$")
# `kind = "Secret"` in HCL or `kind: Secret` in an embedded YAML manifest.
MANIFEST_SECRET = re.compile(r'\bkind\s*[=:]\s*"?Secret"?\s*$')
LOCAL_KEY = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*=")


def blank_view(text: str) -> str:
    """Same length as `text`: comments and string text become spaces, interpolations stay.

    Newlines are preserved so offsets and line numbers still match the original.
    Heredoc bodies are blanked completely (this configuration uses none).
    """
    out = list(text)
    length = len(text)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    def string(i: int) -> int:
        """`i` is just past the opening quote; returns the index past the closing quote."""
        segment = i
        while i < length:
            ch = text[i]
            if ch == "\\":
                i += 2
            elif ch == '"':
                blank(segment, i)
                return i + 1
            elif text.startswith(("$${", "%%{"), i):
                i += 3
            elif text.startswith(("${", "%{"), i):
                blank(segment, i + 2)
                i = code(i + 2, nested=True)
                blank(i - 1, i)  # the closing brace of the interpolation
                segment = i
            else:
                i += 1
        return i

    def heredoc(start: int) -> int | None:
        match = re.compile(r"<<-?([A-Za-z_][\w]*)[ \t]*\n").match(text, start)
        if not match:
            return None
        tag = match.group(1)
        i = match.end()
        segment = i
        while i < length:
            end = text.find("\n", i)
            line_end = length if end < 0 else end
            if text[i:line_end].strip() == tag:
                blank(segment, i)
                return line_end
            i = line_end + 1
        blank(segment, length)
        return length

    def code(i: int, nested: bool) -> int:
        depth = 0
        while i < length:
            ch = text[i]
            if ch == "#" or text.startswith("//", i):
                end = text.find("\n", i)
                end = length if end < 0 else end
                blank(i, end)
                i = end
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                end = length if end < 0 else end + 2
                blank(i, end)
                i = end
            elif ch == '"':
                i = string(i + 1)
            elif text.startswith("<<", i) and (end := heredoc(i)) is not None:
                i = end
            elif ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                if nested and depth == 0:
                    return i + 1
                depth -= 1
                i += 1
            else:
                i += 1
        return i

    code(0, nested=False)
    return "".join(out)


def match_brace(view: str, open_index: int) -> int:
    """Index just past the brace that closes the one at `open_index` (in a blank view)."""
    depth = 0
    for position, char in enumerate(view[open_index:], start=open_index):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return position + 1
    return len(view)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class Module:
    """All top-level blocks of the .tf files in one directory."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.blocks: list[dict] = []
        self.files = sorted(directory.glob("*.tf"))
        for path in self.files:
            self._read(path)

    def _read(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        view = blank_view(text)
        for match in BLOCK.finditer(view):
            kind = match.group(1)
            labels = re.findall(r'"([^"]*)"', text[match.start(2) : match.end(2)])
            open_index = match.end() - 1
            end = match_brace(view, open_index)
            self.blocks.append(
                {
                    "kind": kind,
                    "labels": labels,
                    "body": view[open_index + 1 : end - 1],
                    "file": path.name,
                    "line": line_of(text, match.start()),
                }
            )

    def of_kind(self, kind: str) -> list[dict]:
        return [block for block in self.blocks if block["kind"] == kind]


def local_names(body: str) -> list[str]:
    """Top-level attribute names of one `locals` block body (a blank view)."""
    names, depth = [], 0
    for line in body.splitlines():
        if depth == 0:
            found = LOCAL_KEY.match(line)
            if found:
                names.append(found.group(1))
        depth += sum(line.count(c) for c in "{[(") - sum(line.count(c) for c in "}])")
    return names


def check_references(module: Module) -> list[str]:
    problems: list[str] = []

    variables = {b["labels"][0] for b in module.of_kind("variable") if b["labels"]}
    outputs = {b["labels"][0] for b in module.of_kind("output") if b["labels"]}
    resources = {tuple(b["labels"][:2]) for b in module.of_kind("resource") if len(b["labels"]) == 2}
    data = {tuple(b["labels"][:2]) for b in module.of_kind("data") if len(b["labels"]) == 2}
    locals_ = {name for b in module.of_kind("locals") for name in local_names(b["body"])}

    seen: set[tuple] = set()
    for kind, labels in (
        [("variable", (n,)) for n in (b["labels"][0] for b in module.of_kind("variable") if b["labels"])]
        + [("output", (n,)) for n in (b["labels"][0] for b in module.of_kind("output") if b["labels"])]
        + [("resource", tuple(b["labels"])) for b in module.of_kind("resource")]
        + [("data", tuple(b["labels"])) for b in module.of_kind("data")]
    ):
        if (kind, labels) in seen:
            problems.append(f"duplicate {kind} {'.'.join(labels)}")
        seen.add((kind, labels))

    used_variables: set[str] = set()
    for block in module.blocks:
        body = block["body"]
        where = f"{block['file']}:{block['line']}"
        subject = (
            f"output {block['labels'][0]!r}" if block["kind"] == "output" and block["labels"] else f"{block['kind']} block"
        )

        for name in VAR_REF.findall(body):
            if name not in variables:
                problems.append(f"{where}: {subject} uses undeclared variable var.{name}")
            elif block["kind"] != "variable":
                # A variable that only its own validation reads is still unused.
                used_variables.add(name)
        for name in LOCAL_REF.findall(body):
            if name not in locals_:
                problems.append(f"{where}: {subject} uses undeclared local.{name}")

        for kind_, name in DATA_REF.findall(body):
            if (kind_, name) not in data:
                problems.append(f"{where}: {subject} refers to missing data source data.{kind_}.{name}")
        # Data references are removed first so `data.azurerm_x.y` is not read as a resource.
        for rtype, name in RESOURCE_REF.findall(DATA_REF.sub(" ", body)):
            if (rtype, name) not in resources:
                problems.append(f"{where}: {subject} refers to missing resource {rtype}.{name}")

    for name in sorted(variables - used_variables):
        problems.append(f"variable {name!r} is declared but never used")
    if not outputs:
        problems.append("no outputs declared")
    return problems


def check_literals(directory: Path) -> list[str]:
    problems: list[str] = []
    candidates = sorted(directory.glob("*.tf")) + [
        directory / "terraform.tfvars.example",
        directory / "README.md",
    ]
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in GUID.finditer(text):
            guid = match.group(0)
            if guid == NIL_GUID or PUBLIC_CONSTANT_GUIDS.get(guid.lower()) == path.name:
                continue
            problems.append(f"{path.name}:{line_of(text, match.start())}: GUID-shaped literal; use a <<PLACEHOLDER>>")
        if path.suffix == ".tf":
            for match in LONG_HEX.finditer(text):
                problems.append(
                    f"{path.name}:{line_of(text, match.start())}: long hex literal (digest or secret version?); "
                    "operator values must come from variables"
                )
            if "Microsoft.KeyVault/vaults/secrets" in text:
                problems.append(f"{path.name}: Terraform must not manage Key Vault secret values (Microsoft.KeyVault/vaults/secrets)")
            for number, line in enumerate(text.splitlines(), start=1):
                if line.lstrip().startswith(("#", "//")):
                    continue
                if MANIFEST_SECRET.search(line):
                    problems.append(
                        f"{path.name}:{number}: Terraform must not manage a Kubernetes Secret (kind Secret); "
                        "secrets come from Key Vault through the CSI driver, files from a ConfigMap"
                    )
    return problems


def check_no_secret_resources(module: Module) -> list[str]:
    problems = []
    for block in module.blocks:
        if block["kind"] in ("resource", "data", "ephemeral") and block["labels"] and SECRET_KINDS.match(block["labels"][0]):
            what = "Key Vault" if block["labels"][0].startswith("azurerm_") else "Kubernetes"
            problems.append(
                f"{block['file']}:{block['line']}: Terraform must not manage {what} secret values "
                f"({block['kind']} {block['labels'][0]})"
            )
    return problems


def check_example(directory: Path, module: Module) -> list[str]:
    """The example files must assign every variable without a default, and nothing else."""
    files = [directory / "terraform.tfvars.example", directory / "image.auto.tfvars"]
    if not files[0].exists():
        return []  # fixtures without an example file
    assigned: set[str] = set()
    for path in files:
        if path.exists():
            assigned.update(local_names(blank_view(path.read_text(encoding="utf-8"))))
    declared = {b["labels"][0] for b in module.of_kind("variable") if b["labels"]}
    required = {
        b["labels"][0]
        for b in module.of_kind("variable")
        if b["labels"] and "default" not in local_names(b["body"])
    }
    problems = [f"terraform.tfvars.example / image.auto.tfvars do not assign required variable {n!r}" for n in sorted(required - assigned)]
    problems += [f"terraform.tfvars.example / image.auto.tfvars assign unknown variable {n!r}" for n in sorted(assigned - declared)]
    return problems


def check(directory: Path) -> list[str]:
    module = Module(directory)
    if not module.files:
        return [f"no .tf files in {directory}"]
    return (
        check_references(module)
        + check_literals(directory)
        + check_no_secret_resources(module)
        + check_example(directory, module)
    )


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    problems = check(directory)
    for problem in problems:
        print(f"check_refs: {problem}", file=sys.stderr)
    if problems:
        print(f"check_refs: {len(problems)} problem(s) in {directory}", file=sys.stderr)
        return 1
    print(f"check_refs: ok ({directory})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
