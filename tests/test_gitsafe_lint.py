"""No supervisor code runs git on a tree a worker touched except through ``gitsafe``.

AST based, so it does not depend on one spelling. It is a safety net, not a proof: it finds the spellings
below and says, in the next section, what it cannot see. It flags

* every list or tuple literal whose first element names git: the string ``"git"``, a path whose last
  component is ``git`` (``"/usr/bin/git"``), or a name bound to such a string literal in the same module
  (``GIT = "git"``, at module level or local, one level and no further), wherever the list is built and
  whichever function consumes it (``["git", "status"]``, ``["git", *args]``, ``["git"]`` extended later,
  ``subprocess.run(args=[...])``, ``os.execvp``);
* every list or tuple literal that starts a shell with a command string that runs git
  (``["sh", "-c", "cd x && git status"]``; ``sh``, ``bash``, ``dash`` or ``zsh``, by name or path, with ``-c``
  or ``-lc``);
* every string that has a git word anywhere in it, handed as the first positional argument or as
  ``args=``/``command=``/``cmd=`` to a command runner: ``subprocess.run``, ``Popen``, ``check_output``,
  ``check_call``, ``call``, ``getoutput``, ``getstatusoutput``, ``os.system``, ``os.popen``,
  ``shell.command``/``command``, under whatever name the call site binds it (``from subprocess import run as
  r``, ``r = subprocess.run``), and any call at all that passes ``shell=True``. The string is split on
  whitespace and on ``;``, ``&``, ``|``, ``(``, ``)`` and the backtick, quotes are dropped, a ``NAME=value``
  word is a variable assignment and never a program, and any other word whose last path component is ``git``
  counts (``cd x && git status``, ``GIT_PAGER=cat git status``, ``/usr/bin/git status``, ``sudo git status``,
  ``$(git status)``). The string is a literal, an f-string, ``"git %s" % x``, ``"git {}".format(x)``, a ``+``
  chain (each literal part is read; a part decided at run time is a hole) or a name bound to one of those;
* the same string turned into an argv by ``.split()`` or ``shlex.split()``, directly or through a name;
* ``os.exec*``/``os.spawn*`` calls that name git (``"git"``, a path ending in ``git``, a constant) as an
  argument.

A list inside a comparison (``error.cmd[:2] != ["git", "push"]``) names a command, it does not run one, and
is not flagged. ``gitsafe.py`` itself is exempt.

What the lint cannot see (each item is pinned by ``LIMITS`` below, so a test fails when the scanner starts to
see it, and ``docs/pod-backend.md`` carries the same words; the check is ``test_the_limits_are_stated``):

* a command string built at run time (a word git assembled from parts, a call result, a string returned by a
  function);
* a helper that takes the command as a parameter (``def sh(c): subprocess.run(c, shell=True)`` called with
  ``sh("git status")``: the literal and the runner are in different functions, and the scanner does not follow
  a value into a parameter);
* a command read from data (the environment, a file, a configuration, a database row);
* a program found at run time (``GIT = shutil.which("git")``, ``self.git``, ``settings.GIT``, an imported
  constant, a constant bound to another name);
* a wrapper such as env or sudo ahead of git in a list (``["env", "git", "status"]``; in a string it is seen);
* a runner the scanner does not know by name (``asyncio.create_subprocess_exec``, ``pexpect.spawn``, an
  attribute called something else);
* shell quoting or expansion that spells git without writing it (``$GIT status``, ``g''it status``).

No git call of those kinds exists in ``src/factory`` today (the one command read from data, ``FACTORY_TOKEN_BROKER``
in ``github_auth.py``, is the operator's own token program). What the scanner finds there is the explicit list
below: calls in the supervisor's OWN clone (or in a fresh directory it is about to turn into the clone), each with
the reason it is not a worker-touched tree. A reviewer is still the check on a call the lint cannot see.
"""
import ast
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "factory"
EXEMPT = {"gitsafe.py"}
CONSUMERS = {"run", "Popen", "check_output", "check_call", "call", "command", "getoutput", "getstatusoutput", "system", "popen"}
COMMAND_KEYWORDS = {"args", "command", "cmd"}
PLACEHOLDER = re.compile(r"[%{]")
EXEC_FAMILY = re.compile(r"^(exec|spawn)[lv]p?e?$")
WORD_BREAKS = re.compile(r"[\s;&|()`]+")
SHELLS = {"sh", "bash", "dash", "zsh"}
SHELL_COMMAND_FLAG = re.compile(r"^-[a-z]*c[a-z]*$")
HOLE = "{}"

# (file, enclosing function, git subcommand or "*" for a spread argument list) -> reason.
ALLOWED = {
    ("cli.py", "ensure_clone", "clone"): "creates the supervisor's own clone in a directory it just made",
    ("cli.py", "prepare_worker_access", "config"): "sets core.sharedRepository on the supervisor's own clone",
    ("cli.py", "git_common_dir", "rev-parse"): "common directory of the supervisor's own clone (the operator's repository)",
    ("cli.py", "run", "rev-parse"): "rev-parse --show-toplevel of the operator's working directory, the repository it supervises",
    ("cli.py", "planner_worktree", "fetch"): "fetch in the supervisor's own clone",
    ("cli.py", "planner_worktree", "show-ref"): "ref lookup in the supervisor's own clone",
    ("cli.py", "planner_worktree", "worktree"): "worktree add runs in the supervisor's own clone; the new tree is registered right after",
    ("cli.py", "append_command", "rev-parse"): "rev-parse --show-toplevel of the operator's working directory, the repository it supervises",
}

# What the scanner cannot see: limit -> spellings of git it does not flag. The module docstring and
# docs/pod-backend.md name every limit with these words (test_the_limits_are_stated); a scanner that starts to see
# one fails test_the_documented_limits_are_not_seen until the limit, the docstring and the design record change.
LIMITS = {
    "a command string built at run time": (
        "def f(suffix):\n    os.system('gi' + suffix + ' status')\n",
        "def f():\n    os.system(build_command())\n",
        "def f(parts):\n    os.system(' '.join(parts))\n",
    ),
    "a helper that takes the command as a parameter": (
        "def sh(c):\n    subprocess.run(c, shell=True)\ndef f():\n    sh('git status')\n",
        "class T:\n    def sh(self, c):\n        subprocess.run(c, shell=True)\n    def f(self):\n        self.sh('git status')\n",
    ),
    "a command read from data": (
        "def f():\n    os.system(os.environ['HOOK'])\n",
        "def f(config):\n    command = config['command']\n    subprocess.run(command, shell=True)\n",
        "def f(text):\n    subprocess.run(json.loads(text)['argv'])\n",
    ),
    "a program found at run time": (
        "GIT = shutil.which('git')\ndef f():\n    subprocess.run([GIT, 'status'])\n",
        "def f(self):\n    subprocess.run([self.git, 'status'])\n",
        "def f():\n    subprocess.run([settings.GIT, 'status'])\n",
        "from tools import GIT\ndef f():\n    subprocess.run([GIT, 'status'])\n",
        "GIT = 'git'\nBINARY = GIT\ndef f():\n    subprocess.run([BINARY, 'status'])\n",
        "GIT = 'git'\ndef f():\n    subprocess.run(f'{GIT} status', shell=True)\n",
    ),
    "a wrapper such as env or sudo ahead of git in a list": (
        "def f():\n    subprocess.run(['env', 'git', 'status'])\n",
        "def f():\n    subprocess.run(['sudo', '-u', 'x', 'git', 'status'])\n",
    ),
    "a runner the scanner does not know by name": (
        "async def f():\n    await asyncio.create_subprocess_shell('git status')\n",
        "async def f():\n    await asyncio.create_subprocess_exec('git', 'status')\n",
        "def f():\n    pexpect.spawn('git status')\n",
        "def f(self):\n    self.executor.submit_command('git status')\n",
    ),
    "shell quoting or expansion that spells git without writing it": (
        "def f():\n    os.system('$GIT status')\n",
        "def f():\n    os.system(\"g''it status\")\n",
    ),
}


def consumer_name(call, aliases=None):
    """The name a call site runs, with an alias (``from subprocess import run as r``) resolved."""
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return (aliases or {}).get(function.id, function.id)
    return None


def literal_text(node):
    """The text of a string expression, ``{}`` standing for each part only known at run time, else None.

    Reads a string literal, an f-string, ``left + right``, ``"..." % x`` and ``"...".format(x)``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else HOLE for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = [literal_text(node.left), literal_text(node.right)]
        return None if parts == [None, None] else "".join(HOLE if part is None else part for part in parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return literal_text(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return literal_text(node.func.value)
    return None


def basename(program):
    """The last path component of a program name (``/usr/bin/git`` is ``git``)."""
    return program.rstrip("/").rsplit("/", 1)[-1]


def git_words(text):
    """The words of a shell string from its first git word on, else None.

    The string is split on whitespace, ``; & | ( )`` and backticks and its quotes are dropped. A word with an ``=``
    is a ``NAME=value`` assignment (or an option), never a program; any other word that ends in ``git`` counts.
    """
    words = [word for word in (item.strip("'\"") for item in WORD_BREAKS.split(text or "")) if word]
    starts = [index for index, word in enumerate(words) if "=" not in word and basename(word) == "git"]
    return words[starts[0]:] if starts else None


def passes_shell(call):
    """True unless every ``shell=`` keyword is a literal false."""
    return any(
        keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
        for keyword in call.keywords
    )


def command_arguments(call):
    """The first positional argument and the ``args``/``command``/``cmd`` keywords."""
    return [*call.args[:1], *(keyword.value for keyword in call.keywords if keyword.arg in COMMAND_KEYWORDS)]


def subcommand(elements):
    """The literal second element of an argv list, ``"*"`` when it is anything else (a spread)."""
    if len(elements) > 1 and isinstance(elements[1], ast.Constant) and isinstance(elements[1].value, str):
        return elements[1].value
    return "*"


def assignment(node):
    """(names, value) of an ``=`` or annotated assignment to plain names, else ``([], None)``."""
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [node.target.id], node.value
    return [], None


class Scanner(ast.NodeVisitor):
    def __init__(self):
        self.found = []
        self.scope = []
        self.aliases = {}  # local name -> the runner it is bound to
        self.strings = {}  # local name -> the words of a git shell string it holds
        self.constants = {}  # name -> the string literal it is bound to (one level)

    def where(self):
        return self.scope[-1] if self.scope else "<module>"

    def visit_FunctionDef(self, node):
        outer = self.constants.copy()  # a constant bound inside a function is not one outside it
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        self.constants = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def collect_constants(self, tree):
        """Module-level names bound to a string literal, known before the first function body is read."""
        for statement in tree.body:
            self.bind_constants(*assignment(statement))

    def bind_constants(self, names, value):
        literal = isinstance(value, ast.Constant) and isinstance(value.value, str)
        for name in names:
            if literal:
                self.constants[name] = value.value
            else:
                self.constants.pop(name, None)

    def program(self, node):
        """The string a call or argv names as its program: a literal or a name bound to one, else None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        return None

    def visit_List(self, node):
        self.argv(node)
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Compare(self, node):
        # ``error.cmd[:2] != ["git", "push"]`` names a command, it does not run one.
        for operand in (node.left, *node.comparators):
            if isinstance(operand, (ast.List, ast.Tuple)):
                for element in operand.elts:
                    self.visit(element)
            else:
                self.visit(operand)

    def argv(self, node):
        program = basename(self.program(node.elts[0]) or "") if node.elts else ""
        if program == "git":
            self.found.append((self.where(), subcommand(node.elts), node.lineno))
        elif program in SHELLS:
            words = self.shell_script(node.elts)
            if words is not None:
                self.record(words, node)

    def shell_script(self, elements):
        """The words of the git command in ``[shell, "-c", <string>]``, else None."""
        flag = self.program(elements[1]) if len(elements) > 2 else None
        if flag is None or not SHELL_COMMAND_FLAG.match(flag):
            return None
        return self.command_words(elements[2])

    def visit_ImportFrom(self, node):
        for item in node.names:
            if item.asname:
                self.aliases[item.asname] = item.name

    def visit_Assign(self, node):
        names, value = assignment(node)
        self.bind_constants(names, value)
        if isinstance(value, (ast.Name, ast.Attribute)):
            bound = value.attr if isinstance(value, ast.Attribute) else self.aliases.get(value.id, value.id)
            if bound in CONSUMERS:
                self.aliases.update({name: bound for name in names})
        elif value is not None:
            words = self.command_words(value)
            if words is not None:
                self.strings.update({name: words for name in names})
        self.generic_visit(node)

    visit_AnnAssign = visit_Assign

    def split_source(self, node):
        """The string a ``.split()`` or ``shlex.split()`` call turns into an argv, else None."""
        if not isinstance(node, ast.Call) or consumer_name(node, self.aliases) != "split":
            return None
        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if literal_text(receiver) is not None or (isinstance(receiver, ast.Name) and receiver.id in self.strings):
                return receiver
        return node.args[0] if node.args else None

    def command_words(self, node):
        """The words of the git command a string expression (or its ``.split()``) spells, else None."""
        source = self.split_source(node) or node
        if isinstance(source, ast.Name):
            return self.strings.get(source.id)
        return git_words(literal_text(source))

    def record(self, words, node):
        literal = len(words) > 1 and not PLACEHOLDER.search(words[1])
        self.found.append((self.where(), words[1] if literal else "*", node.lineno))

    def names_git(self, node):
        return basename(self.program(node) or "") == "git"

    def visit_Call(self, node):
        name = consumer_name(node, self.aliases)
        if name in CONSUMERS or passes_shell(node):
            for argument in command_arguments(node):
                words = self.command_words(argument)
                if words is not None:
                    self.record(words, node)
        elif EXEC_FAMILY.match(name or "") and any(self.names_git(item) for item in node.args):
            self.record(["git"], node)
        self.generic_visit(node)


def scan(source):
    tree = ast.parse(source)
    scanner = Scanner()
    scanner.collect_constants(tree)
    scanner.visit(tree)
    return scanner.found


def scan_tree():
    found = []
    for path in sorted(SOURCE.glob("*.py")):
        if path.name in EXEMPT:
            continue
        for scope, command, line in scan(path.read_text()):
            found.append((path.name, scope, command, line))
    return found


class RepositoryTests(unittest.TestCase):
    def test_every_raw_git_call_is_in_the_supervisors_own_clone(self):
        unlisted = [item for item in scan_tree() if item[:3] not in ALLOWED]
        self.assertEqual(
            unlisted,
            [],
            "a git call that does not go through gitsafe: run it with gitsafe.git (a worker-touched tree) "
            "or gitsafe.repo_git (the supervisor's own clone), or list it in ALLOWED with the reason",
        )

    def test_the_allow_list_has_no_stale_entries(self):
        used = {item[:3] for item in scan_tree()}
        self.assertEqual(sorted(set(ALLOWED) - used), [])

    def test_every_entry_names_a_reason(self):
        for key, reason in ALLOWED.items():
            self.assertGreater(len(reason.split()), 3, key)

    def test_no_worker_facing_module_keeps_a_raw_git_call(self):
        for name in ("runner.py", "recovery.py", "agent.py", "preflight.py", "purge.py"):
            raw = [item for item in scan_tree() if item[0] == name]
            self.assertEqual(raw, [], name)


def argument_names(function):
    """The names of every parameter of ``function``, ``*args`` and ``**kwargs`` included."""
    arguments = function.args
    names = {item.arg for item in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    names.update(item.arg for item in (arguments.vararg, arguments.kwarg) if item is not None)
    return names


def assignment_targets(node):
    """The targets of an assignment statement (``=``, ``+=``, ``: T =``), else an empty list."""
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return [node.target]
    return []


def stored_names(target):
    """The ids of the names an assignment target binds (a tuple target binds several)."""
    return [name.id for name in ast.walk(target) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)]


def reassigned_arguments(source):
    """(function, argument, line) for every plain assignment to one of the function's own arguments."""
    found = []
    functions = [item for item in ast.walk(ast.parse(source)) if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        names = argument_names(function)
        found.extend(
            (function.name, name, node.lineno)
            for node in ast.walk(function)
            for target in assignment_targets(node)
            for name in stored_names(target)
            if name in names
        )
    return found


class ArgumentAssignmentTests(unittest.TestCase):
    """The code-quality bot reports ``args = list(args)`` as an assignment to a function argument."""

    def test_the_checker_finds_each_form(self):
        source = "def f(a, *b, c=1, **d):\n    a = list(a)\n    b += (1,)\n    d: dict = {}\n    e = c\n"
        self.assertEqual(reassigned_arguments(source), [("f", "a", 2), ("f", "b", 3), ("f", "d", 4)])

    def test_gitsafe_and_its_tests_bind_a_new_name_instead(self):
        tests = Path(__file__).resolve().parent
        for path in (SOURCE / "gitsafe.py", tests / "test_gitsafe.py", tests / "test_gitsafe_lint.py"):
            with self.subTest(path=path.name):
                self.assertEqual(reassigned_arguments(path.read_text()), [])


class ScannerTests(unittest.TestCase):
    """The lint finds each spelling (a lint that cannot fail proves nothing)."""

    def flagged(self, source):
        return [(scope, command) for scope, command, _ in scan(source)]

    def test_list_literals_through_each_consumer(self):
        for consumer in ("subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call", "subprocess.call", "shell.command", "command"):
            with self.subTest(consumer=consumer):
                source = f"def f(tree):\n    {consumer}(['git', 'status'], cwd=tree)\n"
                self.assertEqual(self.flagged(source), [("f", "status")])

    def test_a_spread_argument_list(self):
        self.assertEqual(self.flagged("def f(args):\n    run(['git', *args])\n"), [("f", "*")])

    def test_an_argv_built_in_a_variable_or_a_tuple(self):
        source = "def f(tree):\n    argv = ['git', 'diff']\n    run(argv, cwd=tree)\n    run(('git', 'add'))\n"
        self.assertEqual(self.flagged(source), [("f", "diff"), ("f", "add")])

    def test_a_shell_string(self):
        source = "def f(tree):\n    run('git status', shell=True)\n    command(f'git {tree}')\n"
        self.assertEqual(self.flagged(source), [("f", "status"), ("f", "*")])

    def test_an_aliased_import_or_assignment_of_a_runner(self):
        # No ``shell=True`` here: the alias alone has to be resolved, or the call reads as an unknown function.
        headers = (
            "from os import system as r",
            "from subprocess import getoutput as r",
            "import os\nr = os.system",
            "import subprocess\nr = getoutput\nr = r",
        )
        for header in headers:
            with self.subTest(header=header):
                source = f"{header}\ndef f(tree):\n    r('git status')\n"
                self.assertEqual(self.flagged(source), [("f", "status")])
        # An alias of something that is not a runner is not one.
        self.assertEqual(self.flagged("from os import getcwd as r\ndef f():\n    r('git status')\n"), [])

    def test_a_module_alias_and_a_runner_that_is_called_through_an_attribute_chain(self):
        self.assertEqual(self.flagged("import subprocess as sp\ndef f():\n    sp.run('git log', shell=True)\n"), [("f", "log")])
        self.assertEqual(self.flagged("def f(self):\n    self.shell.command('git log')\n"), [("f", "log")])

    def test_a_keyword_only_command(self):
        for keyword in ("args", "command", "cmd"):
            with self.subTest(keyword=keyword):
                source = f"def f():\n    subprocess.run({keyword}='git status', shell=True)\n"
                self.assertEqual(self.flagged(source), [("f", "status")])

    def test_a_keyword_argv_is_reported_once(self):
        self.assertEqual(self.flagged("def f():\n    subprocess.run(args=['git', 'status'])\n"), [("f", "status")])

    def test_os_system_and_the_commands_module_functions(self):
        for call in ("os.system", "subprocess.getoutput", "subprocess.getstatusoutput", "os.popen", "popen"):
            with self.subTest(call=call):
                self.assertEqual(self.flagged(f"def f():\n    {call}('git status')\n"), [("f", "status")])

    def test_a_shell_string_built_by_formatting_or_held_in_a_variable(self):
        for expression in ("'git {}'.format(tree)", "'git %s' % tree", "'git ' + tree", "f'git {tree}'"):
            with self.subTest(expression=expression):
                self.assertEqual(self.flagged(f"def f(tree):\n    os.system({expression})\n"), [("f", "*")])
        source = "def f():\n    cmd = 'git status'\n    os.system(cmd)\n"
        self.assertEqual(self.flagged(source), [("f", "status")])

    def test_any_call_with_shell_true_and_a_git_string_is_flagged(self):
        self.assertEqual(self.flagged("def f():\n    helper('git status', shell=True)\n"), [("f", "status")])
        self.assertEqual(self.flagged("def f():\n    helper(args='git add', shell=1)\n"), [("f", "add")])

    def test_exec_and_spawn_that_name_git(self):
        self.assertEqual(self.flagged("def f():\n    os.spawnlp(os.P_WAIT, 'git', 'git', 'status')\n"), [("f", "*")])
        self.assertEqual(self.flagged("def f():\n    os.execlp('git', 'git', 'status')\n"), [("f", "*")])
        self.assertEqual(self.flagged("def f():\n    os.execvp('ls', ['ls'])\n"), [])

    def test_other_shell_strings_are_not_flagged(self):
        sources = (
            "def f():\n    os.system('ls -l')\n",
            "def f():\n    os.system('gitk')\n",
            "def f():\n    run('gitlab status', shell=True)\n",
            "def f():\n    print('git status')\n    log.info('git push failed')\n",
            "def f():\n    cmd = 'ls'\n    os.system(cmd)\n",
            "def f(x):\n    os.system(x)\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [])

    def test_a_comparison_with_an_argv_is_not_a_call(self):
        source = "def f(error):\n    if list(error.cmd)[:2] != ['git', 'push']:\n        raise\n"
        self.assertEqual(self.flagged(source), [])

    def test_other_programs_and_strings_are_not_flagged(self):
        source = "def f():\n    run(['gh', 'api'])\n    text = 'git status'\n    print(['gitsafe'])\n"
        self.assertEqual(self.flagged(source), [])

    def test_a_call_inside_a_nested_function_is_attributed_to_it(self):
        source = "def outer():\n    def inner():\n        run(['git', 'log'])\n"
        self.assertEqual(self.flagged(source), [("inner", "log")])

    def test_a_git_word_anywhere_in_a_shell_string(self):
        texts = (
            "cd x && git status",
            "GIT_PAGER=cat git status",
            "/usr/bin/git status",
            "env GIT_PAGER=cat /usr/local/bin/git status",
            "sudo git status",
            "ls;git status",
            "ls&git status",
            "ls&&git status",
            "ls|git status",
            "(git status)",
            "echo `git status`",
            "echo $(git status)",
            "'git' status",
            'sh -c "git status"',
            "  git   status",
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(self.flagged(f"def f():\n    os.system({text!r})\n"), [("f", "status")])

    def test_a_git_word_that_ends_a_group_or_a_substitution(self):
        for text in ("echo $(which git)", "echo `which git`"):
            with self.subTest(text=text):
                self.assertEqual(self.flagged(f"def f():\n    os.system({text!r})\n"), [("f", "*")])

    def test_a_shell_string_through_each_runner_and_keyword(self):
        for call in ("subprocess.run('cd x && git log', shell=True)", "subprocess.check_output('cd x && git log', shell=True)", "os.popen('cd x && git log')", "shell.command(cmd='cd x && git log')", "helper(args='cd x && git log', shell=True)"):
            with self.subTest(call=call):
                self.assertEqual(self.flagged(f"def f():\n    {call}\n"), [("f", "log")])

    def test_a_variable_assignment_is_not_a_program(self):
        self.assertEqual(self.flagged("def f():\n    os.system('HELPER=/usr/bin/git make')\n"), [])
        self.assertEqual(self.flagged("def f():\n    os.system('PAGER=cat git log')\n"), [("f", "log")])

    def test_a_shell_string_is_read_through_every_literal_part(self):
        expressions = (
            "f'cd {tree} && git status'",
            "'cd %s && git status' % tree",
            "'cd ' + tree + ' && git status'",
            "'cd {} && git status'.format(tree)",
            "f'{tree} git status'",
            "'gi' + 't status'",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                self.assertEqual(self.flagged(f"def f(tree):\n    os.system({expression})\n"), [("f", "status")])
        self.assertEqual(self.flagged("def f(a, b):\n    os.system('cd {} && git {}'.format(a, b))\n"), [("f", "*")])

    def test_a_string_without_a_git_word_is_not_flagged_by_the_new_word_rule(self):
        sources = (
            "def f():\n    os.system('cd /usr/share/gitweb && ls')\n",
            "def f():\n    os.system('echo gitk; ls .git')\n",
            "def f():\n    os.system('ls /home/user/.git')\n",
            "def f(tree):\n    os.system(f'cd {tree} && ls')\n",
            "def f():\n    print('cd x && git status')\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [])

    def test_an_argv_whose_program_is_a_path_to_git(self):
        for program in ("/usr/bin/git", "/usr/local/bin/git", "./git", "bin/git"):
            with self.subTest(program=program):
                self.assertEqual(self.flagged(f"def f():\n    run(['{program}', 'status'])\n"), [("f", "status")])
        self.assertEqual(self.flagged("def f():\n    run(('/usr/bin/git', 'add'))\n"), [("f", "add")])
        self.assertEqual(self.flagged("def f(args):\n    run(['/usr/bin/git', *args])\n"), [("f", "*")])
        for other in ("/usr/bin/gitk", "/usr/share/git/templates", "/home/user/.git", "gitsafe"):
            with self.subTest(other=other):
                self.assertEqual(self.flagged(f"def f():\n    run(['{other}', 'status'])\n"), [])

    def test_a_shell_started_with_a_command_string_that_runs_git(self):
        for shell in ("sh", "bash", "dash", "zsh", "/bin/sh", "/usr/bin/bash"):
            with self.subTest(shell=shell):
                self.assertEqual(self.flagged(f"def f():\n    run(['{shell}', '-c', 'git status'])\n"), [("f", "status")])
        for flag in ("-c", "-lc", "-ec"):
            with self.subTest(flag=flag):
                self.assertEqual(self.flagged(f"def f():\n    run(['sh', '{flag}', 'git status'])\n"), [("f", "status")])
        sources = (
            ("def f():\n    run(('sh', '-c', 'cd x && git status'))\n", "status"),
            ("def f(x):\n    run(['bash', '-c', f'git {x}'])\n", "*"),
            ("def f():\n    cmd = 'git status'\n    run(['sh', '-c', cmd])\n", "status"),
            ("def f():\n    run(args=['sh', '-c', 'git add .'])\n", "add"),
        )
        for source, command in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [("f", command)])

    def test_a_shell_started_with_something_else_is_not_flagged(self):
        sources = (
            "def f():\n    run(['sh', '-c', 'ls'])\n",
            "def f():\n    run(['sh', 'script.sh', 'git status'])\n",
            "def f():\n    run(['sh', '-x', 'git status'])\n",
            "def f():\n    run(['sh', '-c'])\n",
            "def f(cmd):\n    run(['sh', '-c', cmd])\n",
            "def f():\n    run(['python', '-c', 'git status'])\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [])

    def test_a_string_turned_into_an_argv_by_split(self):
        calls = (
            "subprocess.run('git status'.split())",
            "subprocess.run('git status'.split(' '))",
            "subprocess.run('/usr/bin/git status'.split())",
            "subprocess.run(shlex.split('git status'))",
            "subprocess.run(shlex.split('cd x && git status'))",
            "subprocess.run(shlex.split(f'git {tree}'))",
            "subprocess.run(split('git status'))",
            "subprocess.run(sx.split('git status'))",
            "subprocess.run(args='git status'.split())",
            "shell.command('git status'.split())",
        )
        for call in calls:
            with self.subTest(call=call):
                self.assertEqual(self.flagged(f"def f(tree):\n    {call}\n"), [("f", "*" if "{tree}" in call else "status")])

    def test_a_split_string_held_in_a_name_or_behind_an_alias(self):
        sources = (
            "def f():\n    cmd = 'git status'\n    run(cmd.split())\n",
            "def f():\n    argv = 'git status'.split()\n    run(argv)\n",
            "def f():\n    argv = shlex.split('git status')\n    run(argv)\n",
            "from shlex import split as s\ndef f():\n    run(s('git status'))\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [("f", "status")])

    def test_a_split_of_something_else_is_not_flagged(self):
        sources = (
            "def f():\n    run('ls -l'.split())\n",
            "def f():\n    run(shlex.split('ls'))\n",
            "def f(path):\n    run(os.path.split(path))\n",
            "def f():\n    print('git status'.split())\n",
            "def f(text):\n    run(text.split())\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [])

    def test_a_constant_bound_to_git_is_the_program(self):
        sources = (
            "GIT = 'git'\ndef f():\n    run([GIT, 'status'])\n",
            "GIT = 'git'\ndef f():\n    run((GIT, 'status'))\n",
            "def f():\n    GIT = 'git'\n    run([GIT, 'status'])\n",
            "def f():\n    run([GIT, 'status'])\nGIT = 'git'\n",
            "GIT: str = 'git'\ndef f():\n    run([GIT, 'status'])\n",
            "def f():\n    GIT: str = 'git'\n    run([GIT, 'status'])\n",
            "GIT = '/usr/bin/git'\ndef f():\n    run([GIT, 'status'])\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [("f", "status")])
        self.assertEqual(self.flagged("GIT = 'git'\ndef f(args):\n    os.execv(GIT, args)\n"), [("f", "*")])
        self.assertEqual(self.flagged("def f(args):\n    os.execv('/usr/bin/git', args)\n"), [("f", "*")])

    def test_a_constant_is_one_level_of_one_scope(self):
        sources = (
            "def a():\n    GIT = 'git'\ndef b():\n    run([GIT, 'status'])\n",  # a local constant ends with its function
            "GIT = 'git'\ndef f(other):\n    GIT = other\n    run([GIT, 'status'])\n",  # rebound to something unknown
            "GIT = 'git'\nGIT = which('git')\ndef f():\n    run([GIT, 'status'])\n",
            "GIT = 'gh'\ndef f():\n    run([GIT, 'api'])\n",
            "GIT = 'git'\ndef f():\n    run(['gh', GIT])\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(self.flagged(source), [])

    def test_the_documented_limits_are_not_seen(self):
        for limit, sources in LIMITS.items():
            for source in sources:
                with self.subTest(limit=limit, source=source):
                    self.assertEqual(self.flagged(source), [])

    def test_a_wrapper_is_seen_in_a_string_and_not_in_a_list(self):
        self.assertEqual(self.flagged("def f():\n    os.system('env git status')\n"), [("f", "status")])
        self.assertEqual(self.flagged("def f():\n    run(['env', 'git', 'status'])\n"), [])


class DocumentedLimitsTests(unittest.TestCase):
    def test_the_limits_are_stated(self):
        record = Path(__file__).resolve().parent.parent / "docs" / "pod-backend.md"
        texts = {"module docstring": __doc__, "design record": record.read_text(encoding="utf-8")}
        for where, text in texts.items():
            flat = " ".join(text.split())
            for phrase in (*LIMITS, "a safety net, not a proof"):
                with self.subTest(where=where, phrase=phrase):
                    self.assertIn(phrase, flat)


if __name__ == "__main__":
    unittest.main()
