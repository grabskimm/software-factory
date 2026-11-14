#!/usr/bin/env python3
"""Insert (or find, or replace) one entry in the ``repo_configs`` map of an INFRA_TFVARS text.

Used by scripts/register-repo.sh; stdlib only; reads and writes files, never the network.
INFRA_TFVARS is the HCL text of the Terraform inputs (infra/terraform.tfvars.example). The map
looks like this and everything else in the text is none of this tool's business::

    repo_configs = {
      "org/name" = <<-TOML
        repo = "org/name"
        ...
      TOML
    }

Usage::

    tfvars_repo_configs.py plan --raw FILE --key ORG/NAME --entry FILE [--replace] [--out FILE]
                                [--ignore-key TOMLKEY ...]

``--raw`` is what ``gh variable list --jq`` printed: the value plus ONE trailing newline that gh
adds, which is removed again. ``--entry`` is the TOML text of the new entry (unindented).

Prints one JSON object on stdout (and only that)::

    {"status": "inserted" | "unchanged" | "differs" | "replaced",
     "diff": "<unified diff, see below>", "size": <bytes of the resulting text>}

- ``inserted`` / ``replaced``: ``--out`` receives the new text, which equals the old one byte for
  byte except for the added or replaced entry (when a replaced entry shares its last line with a
  comma, a comment or the next entry, those move to their own line: the heredoc terminator must
  stand alone; a comma that only separated entries is dropped, the line break separates them
  too); ``diff`` is a unified diff of the repo_configs region only: from the start of the
  ``repo_configs`` line to the closing brace itself (anything after the opening or the closing
  brace on its line, such as a comment, is not part of it). Before it returns, the new text is
  tokenised and parsed again and must show every other entry unchanged, this entry equal to the
  requested one and the text outside the map byte for byte the same; otherwise: refused.
- ``unchanged``: an entry for the key exists and has the same content (indentation, CRLF and
  trailing blanks do not count). Nothing is written.
- ``differs``: the entry exists with other content; ``diff`` compares that entry only. Nothing is
  written unless ``--replace`` turns the status into ``replaced``.
- ``--ignore-key K`` (repeatable): when comparing an existing entry with the requested one, the
  TOML assignments of key ``K`` (a one-line value, or an array spread over several lines) are
  left out on both sides, so a field the caller did not ask for does not make an entry differ.
  It never changes what is written for ``inserted`` / ``replaced``.

Exit codes: 0 ok, 2 usage, 3 refused (no unique literal ``repo_configs`` map, text this scanner
cannot read with certainty, a duplicate or case-variant key, an unsafe entry). Messages name a
line number at most and never quote the text: INFRA_TFVARS has no secrets by design
(infra/terraform.tfvars.example) but nothing outside the map is ever printed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys

LIMIT_BYTES = 48 * 1024  # GitHub's limit for one Actions variable; the caller enforces it

KEY_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
IDENT_START = re.compile(r"[A-Za-z_]")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
HEREDOC = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n")
MARKER = "TOML"


class Refused(Exception):
    """The text cannot be handled with certainty; nothing is changed."""


class Token:
    __slots__ = ("kind", "start", "end", "value", "indent_strip")

    def __init__(self, kind, start, end, value=None, indent_strip=False):
        self.kind = kind  # ident string heredoc punct newline other
        self.start = start
        self.end = end
        self.value = value
        self.indent_strip = indent_strip


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def skip_template(text, i):
    """``i`` is just after ``${`` or ``%{``; return the index after the matching ``}``."""
    depth = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i = skip_string(text, i + 1)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Refused("unterminated template sequence near line %d" % line_of(text, n - 1))


def skip_string(text, i):
    """``i`` is just after the opening quote; return the index after the closing quote."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "\n":
            break
        if c in "$%":
            if text.startswith(c + c + "{", i):  # '$${' and '%%{' escape a literal '${' and '%{'
                i += 3
                continue
            if text.startswith("{", i + 1):
                i = skip_template(text, i + 2)
                continue
        i += 1
    raise Refused("unterminated string near line %d" % line_of(text, min(i, n - 1)))


def tokenize(text):
    """Significant tokens only: comments and blanks are dropped, newlines are kept."""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            tokens.append(Token("newline", i, i + 1))
            i += 1
        elif c in " \t\r\f\v﻿":
            i += 1
        elif c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise Refused("unterminated comment near line %d" % line_of(text, i))
            i = j + 2
        elif c == '"':
            j = skip_string(text, i + 1)
            tokens.append(Token("string", i, j, text[i + 1:j - 1]))
            i = j
        elif c == "<" and text.startswith("<<", i) and HEREDOC.match(text, i):
            m = HEREDOC.match(text, i)
            marker, dash = m.group(2), m.group(1) == "-"
            pos = m.end()
            while True:
                nl = text.find("\n", pos)
                line_end = n if nl < 0 else nl
                if text[pos:line_end].strip(" \t\r") == marker:
                    break
                if nl < 0:
                    raise Refused("unterminated heredoc starting at line %d" % line_of(text, i))
                pos = nl + 1
            body = text[m.end():pos]
            term_end = pos + len(text[pos:line_end].rstrip(" \t\r"))  # a CRLF's CR stays outside the token
            tokens.append(Token("heredoc", i, term_end, (marker, body, m.end(), pos), dash))
            i = term_end
        elif IDENT_START.match(c):
            m = IDENT.match(text, i)
            tokens.append(Token("ident", i, m.end(), m.group(0)))
            i = m.end()
        elif c in "{}[]()=:,.":
            tokens.append(Token("punct", i, i + 1, c))
            i += 1
        else:
            tokens.append(Token("other", i, i + 1, c))
            i += 1
    return tokens


OPEN = {"{": "}", "[": "]", "(": ")"}
CLOSE = {"}", "]", ")"}


def is_punct(tok, ch):
    return tok.kind == "punct" and tok.value == ch


def find_close(tokens, open_index):
    """Index of the token that closes the bracket at ``open_index``."""
    stack = []
    for k in range(open_index, len(tokens)):
        tok = tokens[k]
        if tok.kind != "punct":
            continue
        if tok.value in OPEN:
            stack.append(OPEN[tok.value])
        elif tok.value in CLOSE:
            if not stack or stack.pop() != tok.value:
                raise Refused("unbalanced brackets")
            if not stack:
                return k
    raise Refused("unbalanced brackets")


def locate_map(text, tokens):
    """The one top-level ``repo_configs = { ... }``: (index of '{', index of '}')."""
    depth = 0
    found = []
    prev_sig = None  # previous significant token at depth 0, newline counts
    for k, tok in enumerate(tokens):
        if tok.kind == "punct" and tok.value in OPEN:
            depth += 1
        elif tok.kind == "punct" and tok.value in CLOSE:
            depth -= 1
            if depth < 0:
                raise Refused("unbalanced brackets near line %d" % line_of(text, tok.start))
        elif depth == 0 and tok.kind == "ident" and tok.value == "repo_configs":
            if prev_sig is None or prev_sig.kind == "newline":
                found.append(k)
        if depth == 0:
            prev_sig = tok
    if depth != 0:
        raise Refused("unbalanced brackets at the end of the text")
    attrs = []
    for k in found:
        nxt = tokens[k + 1] if k + 1 < len(tokens) else None
        if nxt is not None and is_punct(nxt, "="):
            attrs.append(k)
    if len(attrs) != 1:
        raise Refused("expected exactly one top-level 'repo_configs' attribute, found %d" % len(attrs))
    k = attrs[0]
    k_open = k + 2
    if k_open >= len(tokens) or not is_punct(tokens[k_open], "{"):
        raise Refused("'repo_configs' is not a literal { ... } map (line %d)" % line_of(text, tokens[k].start))
    return k, k_open, find_close(tokens, k_open)


def decode_key(tok):
    """The key's text, or None when it cannot be known without evaluating HCL."""
    if tok.kind == "ident":
        return tok.value
    if tok.kind == "string":
        raw = tok.value
        if "${" in raw or "%{" in raw:
            return None
        return raw.replace('\\"', '"').replace("\\\\", "\\")
    return None


def parse_entries(text, tokens, k_open, k_close):
    """Entries of the map: dicts with key, key token and the last token of the value."""
    entries = []
    k = k_open + 1
    while k < k_close:
        tok = tokens[k]
        if tok.kind == "newline" or is_punct(tok, ","):
            k += 1
            continue
        if tok.kind not in ("string", "ident"):
            raise Refused("unexpected token in repo_configs near line %d" % line_of(text, tok.start))
        key_tok = tok
        k += 1
        if k >= k_close or not (is_punct(tokens[k], "=") or is_punct(tokens[k], ":")):
            raise Refused("expected '=' after a key in repo_configs near line %d" % line_of(text, tok.start))
        k += 1
        while k < k_close and tokens[k].kind == "newline":
            k += 1
        if k >= k_close:
            raise Refused("a repo_configs entry without a value near line %d" % line_of(text, tok.start))
        first = tokens[k]
        if first.kind in ("heredoc", "string"):
            last = k
            k += 1
        else:
            depth = 0
            last = k
            while k < k_close:
                t = tokens[k]
                if t.kind == "punct" and t.value in OPEN:
                    depth += 1
                elif t.kind == "punct" and t.value in CLOSE:
                    depth -= 1
                elif depth == 0 and (t.kind == "newline" or is_punct(t, ",")):
                    break
                last = k
                k += 1
        entries.append({"key": decode_key(key_tok), "key_tok": key_tok, "value_index": last,
                        "first_value": first, "last_tok": tokens[last]})
    return entries


def line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def indent_of_line(text, pos):
    ls = line_start(text, pos)
    m = re.match(r"[ \t]*", text[ls:])
    return m.group(0)


def dedent(lines):
    """HCL's ``<<-`` rule: strip the smallest indentation of the non-blank lines."""
    indents = [len(x) - len(x.lstrip(" \t")) for x in lines if x.strip()]
    cut = min(indents) if indents else 0
    return [x[cut:] if x.strip() else x.lstrip(" \t") for x in lines]


def normalise(lines):
    out = [x.rstrip(" \t\r") for x in lines]
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return out


def existing_lines(tok_first, text):
    """Normalised content lines of an existing entry, or its raw text when not a heredoc."""
    if tok_first.kind == "heredoc":
        _marker, body, _a, _b = tok_first.value
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = [x.rstrip("\r") for x in lines]
        if tok_first.indent_strip:
            lines = dedent(lines)
        return normalise(lines)
    return normalise(text[tok_first.start:tok_first.end].splitlines())


def drop_keys(lines, keys):
    """``lines`` without the TOML assignments of ``keys`` (arrays may span lines)."""
    if not keys:
        return lines
    out = []
    pending = 0
    for x in lines:
        if pending > 0:
            pending += x.count("[") - x.count("]")
            continue
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*=(.*)$", x)
        if m and m.group(1) in keys:
            pending = max(0, m.group(2).count("[") - m.group(2).count("]"))
            continue
        out.append(x)
    return normalise(out)


def check_entry(entry_lines):
    if not entry_lines or not any(x.strip() for x in entry_lines):
        raise Refused("the entry is empty")
    for x in entry_lines:
        if "\x00" in x or "\r" in x:
            raise Refused("the entry contains a control character")
        if "${" in x or "%{" in x:
            raise Refused("the entry contains a template sequence")
        if x.strip() == MARKER:
            raise Refused("the entry contains the heredoc terminator line")


def render_entry(key, entry_lines, indent):
    inner = indent + "  "
    out = ['%s"%s" = <<-%s' % (indent, key, MARKER)]
    for x in entry_lines:
        out.append(inner + x if x else "")
    out.append("%s%s" % (indent, MARKER))
    return out


def detect_eol(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def unified(old_lines, new_lines, old_name, new_name):
    return "\n".join(difflib.unified_diff(
        [x.rstrip("\r") for x in old_lines], [x.rstrip("\r") for x in new_lines],
        fromfile=old_name, tofile=new_name, lineterm="", n=3))


def region_diff(text, new_text, region_start, open_end, close_pos):
    """Unified diff of the map: from the start of the ``repo_configs`` line to the closing brace.

    Everything the changes touch lies before the closing brace, so it moves by the change in
    length. Cutting at the brace itself keeps a comment behind it out of the output, and a
    line comment behind the opening brace is cut as well."""
    shift = len(new_text) - len(text)
    nl = text.find("\n", open_end)
    cut = None
    if 0 <= nl < close_pos and text[open_end:nl].strip(" \t\r").startswith(("#", "//")):
        cut = (open_end, nl)

    def scrub(t, end):
        part = t[region_start:end]
        if cut:
            part = part[:cut[0] - region_start] + part[cut[1] - region_start:]
        return part.splitlines()

    return unified(scrub(text, close_pos + 1), scrub(new_text, close_pos + shift + 1),
                   "INFRA_TFVARS repo_configs (current)", "INFRA_TFVARS repo_configs (new)")


def entry_signature(text, entry):
    first, last = entry["first_value"], entry["last_tok"]
    return (entry["key"], re.sub(r"[ \t\r]+", " ", text[first.start:last.end]))


def self_check(text, new_text, key, wanted, others_before):
    """Re-read ``new_text`` and require exactly the intended change; raise Refused otherwise."""
    try:
        tokens = tokenize(new_text)
        _k, k_open, k_close = locate_map(new_text, tokens)
        entries = parse_entries(new_text, tokens, k_open, k_close)
    except Refused as exc:
        raise Refused("internal check failed, the edited text would not parse (%s)" % exc)
    old_tokens = tokenize(text)
    _k, o_open, o_close = locate_map(text, old_tokens)
    if text[:old_tokens[o_open].end] != new_text[:tokens[k_open].end]:
        raise Refused("internal check failed, the text before the map would change")
    if text[old_tokens[o_close].start:] != new_text[tokens[k_close].start:]:
        raise Refused("internal check failed, the text after the map would change")
    lowered = key.lower()
    mine = [e for e in entries if e["key"] is not None and e["key"].lower() == lowered]
    if len(mine) != 1 or mine[0]["key"] != key:
        raise Refused("internal check failed, the edited text does not hold the entry exactly once")
    if existing_lines(mine[0]["first_value"], new_text) != wanted or mine[0]["first_value"].kind != "heredoc":
        raise Refused("internal check failed, the edited entry differs from the requested one")
    others_after = [entry_signature(new_text, e) for e in entries if e is not mine[0]]
    if others_after != others_before:
        raise Refused("internal check failed, another entry of repo_configs would change")


def plan(raw, key, entry_text, replace, ignore=()):
    """Return (status, diff, new_text) for the INFRA_TFVARS text ``raw`` (gh's output)."""
    if not KEY_RE.fullmatch(key) or ".." in key:
        raise Refused("invalid key")
    text = raw[:-1] if raw.endswith("\n") else raw
    entry_lines = [x.rstrip(" \t") for x in entry_text.replace("\r\n", "\n").split("\n")]
    while entry_lines and entry_lines[-1] == "":
        entry_lines.pop()
    check_entry(entry_lines)

    tokens = tokenize(text)
    k_attr, k_open, k_close = locate_map(text, tokens)
    entries = parse_entries(text, tokens, k_open, k_close)
    eol = detect_eol(text)

    lowered = key.lower()
    same = [e for e in entries if e["key"] is not None and e["key"].lower() == lowered]
    if len(same) > 1:
        raise Refused("the key appears more than once in repo_configs")
    if same and same[0]["key"] != key:
        raise Refused("repo_configs already has this repository under a key with different letter case")

    map_indent = indent_of_line(text, tokens[k_attr].start)
    region_start = line_start(text, tokens[k_attr].start)
    close_pos = tokens[k_close].start
    wanted = normalise(entry_lines)
    others_before = [entry_signature(text, e) for e in entries if not same or e is not same[0]]

    if not same:
        key_indent = None
        for e in entries:
            ls = line_start(text, e["key_tok"].start)
            if text[ls:e["key_tok"].start].strip(" \t") == "":
                key_indent = text[ls:e["key_tok"].start]
                break
        ls = line_start(text, close_pos)
        own_line = text[ls:close_pos].strip(" \t") == ""
        if key_indent is None:
            key_indent = (text[ls:close_pos] if own_line else map_indent) + "  "
        lines = render_entry(key, entry_lines, key_indent)
        if own_line:
            at = ls
            insertion = eol.join(lines) + eol
        else:
            at = close_pos
            insertion = eol + eol.join(lines) + eol + map_indent
        new_text = text[:at] + insertion + text[at:]
        self_check(text, new_text, key, wanted, others_before)
        return "inserted", region_diff(text, new_text, region_start, tokens[k_open].end, close_pos), new_text

    entry = same[0]
    first = entry["first_value"]
    current = existing_lines(first, text)
    ignored = set(ignore)
    cur_cmp, want_cmp = drop_keys(current, ignored), drop_keys(wanted, ignored)
    if cur_cmp == want_cmp:
        return "unchanged", "", text
    entry_diff = unified(cur_cmp, want_cmp, 'existing entry "%s"' % key, 'requested entry "%s"' % key)
    if not replace:
        return "differs", entry_diff, text

    key_tok = entry["key_tok"]
    last_tok = entry["last_tok"]
    ls = line_start(text, key_tok.start)
    own_start = text[ls:key_tok.start].strip(" \t") == ""
    indent = text[ls:key_tok.start] if own_start else ""
    lines = render_entry(key, entry_lines, indent)
    start = ls if own_start else key_tok.start
    if not own_start:
        lines[0] = lines[0].lstrip(" \t")
    replacement = eol.join(lines)
    # What follows the old value on its line (a comma, a comment, the next entry, the closing
    # brace) cannot stay behind the terminator: the heredoc terminator must stand alone on its
    # line. It moves to a line of its own; a comma that only separated entries is dropped (the
    # line break separates them as well).
    line_end = text.find("\n", last_tok.end)
    line_end = len(text) if line_end < 0 else line_end
    rest = text[last_tok.end:line_end].strip(" \t\r")
    if rest:
        if rest.startswith(","):
            rest = rest[1:].strip(" \t")
        rest_indent = map_indent if rest.startswith("}") else (indent if own_start else map_indent + "  ")
        if rest:
            replacement += eol + rest_indent + rest
        tail_start = line_end - 1 if text[line_end - 1:line_end] == "\r" else line_end
    else:
        tail_start = last_tok.end
    new_text = text[:start] + replacement + text[tail_start:]
    self_check(text, new_text, key, wanted, others_before)
    return "replaced", region_diff(text, new_text, region_start, tokens[k_open].end, close_pos), new_text


def read_bytes_as_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "surrogateescape")


def write_text(path, text):
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8", "surrogateescape"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--raw", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--entry", required=True)
    p.add_argument("--out")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--ignore-key", action="append", default=[])
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        raw = read_bytes_as_text(args.raw)
        entry = read_bytes_as_text(args.entry)
        status, diff, new_text = plan(raw, args.key, entry, args.replace, args.ignore_key)
    except Refused as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 3
    except OSError as exc:
        sys.stderr.write("cannot read an input file (%s)\n" % exc.__class__.__name__)
        return 1
    if status in ("inserted", "replaced") and args.out:
        write_text(args.out, new_text)
    size = len(new_text.encode("utf-8", "surrogateescape"))
    json.dump({"status": status, "diff": diff, "size": size}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
