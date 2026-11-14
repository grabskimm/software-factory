"""Loads the tools of the live runbook (tools/live_*.py) by path: the tools import each other by their module name, as a script does."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def load(name):
    """The module tools/<name>.py, loaded once under its own name (so `import live_recording` in a tool finds the same module)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def imported_modules(name):
    """The top-level module names the tool imports (by its syntax tree, so a word in a comment or a docstring does not count)."""
    import ast

    found = set()
    for node in ast.walk(ast.parse((TOOLS / f"{name}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def calls(name):
    """The dotted names of every call in the tool (`subprocess.run`, `os.system`, `open`), by its syntax tree."""
    import ast

    def dotted(node):
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return node.id if isinstance(node, ast.Name) else ""

    return {dotted(n.func) for n in ast.walk(ast.parse((TOOLS / f"{name}.py").read_text(encoding="utf-8"))) if isinstance(n, ast.Call)}


def keywords(name):
    """The (call name, keyword, constant value) of every constant keyword argument in the tool: `shell=True` is found here."""
    import ast

    found = set()
    for node in ast.walk(ast.parse((TOOLS / f"{name}.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if isinstance(keyword.value, ast.Constant):
                    found.add((keyword.arg, keyword.value.value))
    return found
