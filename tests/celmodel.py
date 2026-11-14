"""A small offline model of the CEL subset the worker admission policies use (slice 15b of the pod backend).

It exists to run the expressions of ``infra/policy/*.json`` against the golden pod, the golden Secret and every
mutation of ``tests/golden/policy_mutations.json`` without a cluster. It is a MODEL: it implements the CEL
semantics the policies rely on (missing field access is an error, ``has()`` tests a field, ``&&`` and ``||`` absorb an
error on the other side like CEL does, ``all`` and ``exists`` over lists and over map keys, ``exists_one``, ``filter``,
``size``, ``matches`` (partial match), ``startsWith``, ``endsWith``, ``contains``, a map and list literal, the ternary,
``in``, and the Kubernetes quantity library: ``quantity``, ``isQuantity``, ``compareTo``, ``isGreaterThan``,
``isLessThan``). It is NOT evidence that a real API server accepts the expressions: the type check against the pod
schema, the cost estimate and the exact function semantics of the Kubernetes CEL environment are only checked by a run of
``tools/policy_matrix.py`` against a kind cluster (a real API server of the pinned kind version). An error while evaluating is reported to
the caller (CelError); on a real server it would be a denial without the rule id in the message.

Standard library only; no randomness, no clock, no network.
"""

import re
from fractions import Fraction
from functools import lru_cache


class CelError(Exception):
    """An evaluation or parse error (a fixed text)."""


_TOKEN = re.compile(
    r"""\s*(?:
    (?P<num>[0-9]+(?:\.[0-9]+)?)|
    (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|
    (?P<id>[A-Za-z_][A-Za-z_0-9]*)|
    (?P<op>\|\||&&|==|!=|<=|>=|[<>+\-*/%!?:.,()\[\]{}])
    )""",
    re.VERBOSE,
)
_ESCAPES = {"n": "\n", "t": "\t", "'": "'", '"': '"', "\\": "\\"}


def _tokens(text):
    position = 0
    found = []
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise CelError("unexpected character")
        position = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "str":
            body = value[1:-1]
            if "\\" in body:
                body = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body)
            value = body
        elif kind == "num":
            value = float(value) if "." in value else int(value)
        found.append((kind, value))
    found.append(("end", None))
    return found


class _Parser:
    def __init__(self, text):
        self.tokens = _tokens(text)
        self.at = 0

    def peek(self):
        return self.tokens[self.at]

    def take(self):
        token = self.tokens[self.at]
        self.at += 1
        return token

    def accept(self, op):
        kind, value = self.peek()
        if kind == "op" and value == op:
            self.at += 1
            return True
        return False

    def expect(self, op):
        if not self.accept(op):
            raise CelError("expected a token")

    def parse(self):
        node = self.ternary()
        if self.peek()[0] != "end":
            raise CelError("trailing tokens")
        return node

    def ternary(self):
        cond = self.or_()
        if self.accept("?"):
            first = self.ternary()
            self.expect(":")
            return ("?:", cond, first, self.ternary())
        return cond

    def or_(self):
        node = self.and_()
        while self.accept("||"):
            node = ("||", node, self.and_())
        return node

    def and_(self):
        node = self.rel()
        while self.accept("&&"):
            node = ("&&", node, self.rel())
        return node

    def rel(self):
        node = self.add()
        while True:
            kind, value = self.peek()
            if kind == "op" and value in ("==", "!=", "<", "<=", ">", ">="):
                self.take()
                node = ("bin", value, node, self.add())
            elif kind == "id" and value == "in":
                self.take()
                node = ("bin", "in", node, self.add())
            else:
                return node

    def add(self):
        node = self.unary()
        while True:
            kind, value = self.peek()
            if kind == "op" and value in ("+", "-"):
                self.take()
                node = ("bin", value, node, self.unary())
            else:
                return node

    def unary(self):
        if self.accept("!"):
            return ("!", self.unary())
        if self.accept("-"):
            return ("neg", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while True:
            if self.accept("."):
                kind, name = self.take()
                if kind != "id":
                    raise CelError("expected a field name")
                if self.accept("("):
                    node = ("call", node, name, self.arguments(")"))
                else:
                    node = ("sel", node, name)
            elif self.accept("["):
                index = self.ternary()
                self.expect("]")
                node = ("idx", node, index)
            else:
                return node

    def arguments(self, closer):
        found = []
        if self.accept(closer):
            return found
        while True:
            found.append(self.ternary())
            if self.accept(closer):
                return found
            self.expect(",")

    def primary(self):
        kind, value = self.take()
        if kind in ("num", "str"):
            return ("lit", value)
        if kind == "id":
            if value in ("true", "false"):
                return ("lit", value == "true")
            if value == "null":
                return ("lit", None)
            if self.accept("("):
                return ("call", None, value, self.arguments(")"))
            return ("id", value)
        if kind == "op" and value == "(":
            node = self.ternary()
            self.expect(")")
            return node
        if kind == "op" and value == "[":
            return ("list", self.arguments("]"))
        if kind == "op" and value == "{":
            entries = []
            if not self.accept("}"):
                while True:
                    key = self.ternary()
                    self.expect(":")
                    entries.append((key, self.ternary()))
                    if self.accept("}"):
                        break
                    self.expect(",")
            return ("map", entries)
        raise CelError("unexpected token")


@lru_cache(maxsize=None)
def compile_expression(text):
    """The syntax tree of `text` (cached); CelError for anything outside the subset."""
    return _Parser(text).parse()


# --- Values ----------------------------------------------------------------------------------------

_QUANTITY = re.compile(r"([0-9]+(?:\.[0-9]+)?)(?:([eE])([+-]?[0-9]+)|(n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei))?")
_FACTORS = {
    "": Fraction(1),
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 1000),
    "k": Fraction(1000),
    "M": Fraction(1000**2),
    "G": Fraction(1000**3),
    "T": Fraction(1000**4),
    "P": Fraction(1000**5),
    "E": Fraction(1000**6),
    "Ki": Fraction(1024),
    "Mi": Fraction(1024**2),
    "Gi": Fraction(1024**3),
    "Ti": Fraction(1024**4),
    "Pi": Fraction(1024**5),
    "Ei": Fraction(1024**6),
}


class Quantity:
    def __init__(self, value):
        self.value = value


def _parse_quantity(text):
    found = _QUANTITY.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        return None
    number = Fraction(found.group(1))
    if found.group(2):
        return Quantity(number * Fraction(10) ** int(found.group(3)))
    return Quantity(number * _FACTORS[found.group(4) or ""])


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_equal(left[key], right[key]) for key in left)
    if type(left) is not type(right):
        return False
    return left == right


def _order(left, right):
    if _is_number(left) and _is_number(right):
        return (left > right) - (left < right)
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    raise CelError("no such overload")


def _bool(value):
    if not isinstance(value, bool):
        raise CelError("a boolean was expected")
    return value


_MISSING = object()


def _select(target, name):
    if isinstance(target, dict):
        if name in target:
            return target[name]
        raise CelError("no such key")
    raise CelError("no such field")


def _index(target, key):
    if isinstance(target, list):
        if isinstance(key, bool) or not isinstance(key, int) or not 0 <= key < len(target):
            raise CelError("index out of range")
        return target[key]
    if isinstance(target, dict):
        if isinstance(key, str) and key in target:
            return target[key]
        raise CelError("no such key")
    raise CelError("not indexable")


def _members(target):
    if isinstance(target, list):
        return list(target)
    if isinstance(target, dict):
        return list(target)
    raise CelError("not iterable")


class _Evaluator:
    def __init__(self, env):
        self.env = env

    def run(self, node, scope):
        kind = node[0]
        method = getattr(self, f"_{kind.replace('?:', 'ternary').replace('||', 'or').replace('&&', 'and').replace('!', 'not')}")
        return method(node, scope)

    def _lit(self, node, scope):
        return node[1]

    def _id(self, node, scope):
        name = node[1]
        if name in scope:
            return scope[name]
        if name in self.env:
            return self.env[name]
        raise CelError("undeclared reference")

    def _list(self, node, scope):
        return [self.run(item, scope) for item in node[1]]

    def _map(self, node, scope):
        result = {}
        for key, value in node[1]:
            result[self.run(key, scope)] = self.run(value, scope)
        return result

    def _sel(self, node, scope):
        return _select(self.run(node[1], scope), node[2])

    def _idx(self, node, scope):
        return _index(self.run(node[1], scope), self.run(node[2], scope))

    def _ternary(self, node, scope):
        if _bool(self.run(node[1], scope)):
            return self.run(node[2], scope)
        return self.run(node[3], scope)

    def _not(self, node, scope):
        return not _bool(self.run(node[1], scope))

    def _neg(self, node, scope):
        value = self.run(node[1], scope)
        if not _is_number(value):
            raise CelError("no such overload")
        return -value

    def _both(self, node, scope, absorbing):
        error = None
        for operand in (node[1], node[2]):
            try:
                value = _bool(self.run(operand, scope))
            except CelError as caught:
                error = caught
                continue
            if value is absorbing:
                return absorbing
        if error is not None:
            raise error
        return not absorbing

    def _and(self, node, scope):
        return self._both(node, scope, False)

    def _or(self, node, scope):
        return self._both(node, scope, True)

    def _bin(self, node, scope):
        op = node[1]
        left = self.run(node[2], scope)
        right = self.run(node[3], scope)
        if op == "==":
            return _equal(left, right)
        if op == "!=":
            return not _equal(left, right)
        if op in ("<", "<=", ">", ">="):
            order = _order(left, right)
            return {"<": order < 0, "<=": order <= 0, ">": order > 0, ">=": order >= 0}[op]
        if op == "in":
            if isinstance(right, list):
                return any(_equal(left, item) for item in right)
            if isinstance(right, dict):
                return isinstance(left, str) and left in right
            raise CelError("no such overload")
        if op == "+":
            if isinstance(left, str) and isinstance(right, str):
                return "".join((left, right))
            if isinstance(left, list) and isinstance(right, list):
                return [*left, *right]
            if _is_number(left) and _is_number(right):
                return left + right
            raise CelError("no such overload")
        raise CelError("unsupported operator")

    def _call(self, node, scope):
        target, name, args = node[1], node[2], node[3]
        macro = self._macro(target, name, args, scope)
        if macro is not _MISSING:
            return macro
        if target is None:
            return self._function(name, [self.run(arg, scope) for arg in args])
        return self._method(self.run(target, scope), name, [self.run(arg, scope) for arg in args])

    def _macro(self, target, name, args, scope):
        if target is None and name == "has":
            if len(args) != 1 or args[0][0] != "sel":
                raise CelError("has() needs a field selection")
            holder = self.run(args[0][1], scope)
            if not isinstance(holder, dict):
                raise CelError("has() on a value that is not a message")
            return args[0][2] in holder
        if target is None or name not in ("all", "exists", "exists_one", "filter", "map"):
            return _MISSING
        if len(args) != 2 or args[0][0] != "id":
            raise CelError("a macro needs a variable and an expression")
        variable = args[0][1]
        results = []
        error = None
        items = _members(self.run(target, scope))
        for item in items:
            inner = dict(scope)
            inner[variable] = item
            try:
                results.append((item, self.run(args[1], inner)))
            except CelError as caught:
                error = caught
                results.append((item, caught))
        values = [value for _, value in results]
        if name == "map":
            if error is not None:
                raise error
            return values
        flags = [value for value in values if not isinstance(value, CelError)]
        if name != "filter" and not all(isinstance(flag, bool) for flag in flags):
            raise CelError("a boolean was expected")
        if name == "all":
            if any(flag is False for flag in flags):
                return False
        elif name == "exists":
            if any(flag is True for flag in flags):
                return True
        if error is not None:
            raise error
        if name == "all":
            return True
        if name == "exists":
            return False
        if name == "exists_one":
            return sum(1 for flag in flags if flag is True) == 1
        return [item for item, flag in results if flag is True]  # filter

    def _function(self, name, args):
        if name == "size" and len(args) == 1:
            return self._size(args[0])
        if name == "quantity" and len(args) == 1:
            parsed = _parse_quantity(args[0])
            if parsed is None:
                raise CelError("invalid quantity")
            return parsed
        if name == "isQuantity" and len(args) == 1:
            return _parse_quantity(args[0]) is not None
        raise CelError("unsupported function")

    def _size(self, value):
        if isinstance(value, (list, dict, str, bytes)):
            return len(value)
        raise CelError("no such overload")

    def _method(self, receiver, name, args):
        if name == "size" and not args:
            return self._size(receiver)
        if isinstance(receiver, str) and len(args) == 1 and isinstance(args[0], str):
            if name == "matches":
                return re.search(args[0], receiver) is not None
            if name == "startsWith":
                return receiver.startswith(args[0])
            if name == "endsWith":
                return receiver.endswith(args[0])
            if name == "contains":
                return args[0] in receiver
        if isinstance(receiver, Quantity) and len(args) == 1 and isinstance(args[0], Quantity):
            order = (receiver.value > args[0].value) - (receiver.value < args[0].value)
            if name == "compareTo":
                return order
            if name == "isGreaterThan":
                return order > 0
            if name == "isLessThan":
                return order < 0
        raise CelError("unsupported method")


def evaluate(text, **env):
    """The value of the CEL expression `text` with the variables `env` (object, params, request); CelError on any error."""
    return _Evaluator(env).run(compile_expression(text), {})
