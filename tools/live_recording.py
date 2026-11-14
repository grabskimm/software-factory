#!/usr/bin/env python3
"""The recording format of the live runbook, and the tool that writes and checks one (slice 21 of the pod backend).

docs/pod-backend-runbook.md has a person with Azure access run read-only checks and a throwaway mirror. What they observe
becomes, after review, the test fixtures of slice 22, which replace the assumed shapes of slices 1, 7 and 11 (a pod and
an event at a NodePool limit, the answers of the API server, the `claude` error events, the admission denial). A recording
is ONE json file with exactly these keys:

    format        "factory-runbook-recording/1"
    step          the runbook step that produced it: a part letter A to G and one or two digits ("D5")
    kind          one word of KINDS (below)
    recorded_at   UTC, "YYYY-MM-DDTHH:MM:SSZ"
    tool          which program wrote it (a short fixed string, never a path)
    argv          the command that produced the payload as a list of strings, or null when a person supplied the file
    returncode    its exit status, or null
    redactions    how many values the scrubber replaced or removed
    payload       a JSON document (kind form "json") or {"text": "..."} (kind form "text")
    sha256        the hash of the canonical JSON of the payload, so a hand edit is noticed

The scrubber is a safety net, not a licence: a payload that holds a Kubernetes Secret or SecretList (as an object, or as a
JSON or YAML text inside a string, such as the last-applied-configuration annotation) is REFUSED (never scrubbed), a value
of a credential shape (an Anthropic or GitHub key, a bearer token, a JSON web token, a SAS signature, a long base64 string, a
long hexadecimal string that is not a digest) is replaced, a GUID becomes the zero GUID, an IPv4 address becomes 0.0.0.0 (the two fixed Azure
addresses of the contract's canary stay), an IPv6 address becomes `::`, the value of an environment entry or a key whose name says key, token, secret,
password or authorization is replaced, so is the value of `token: x`, `password=x` or `--token=x` written in a text (and the word after a lone `--token`
in an argument list), and `managedFields` is dropped. `validate` scans a recording again and fails on anything the scrubber would have
changed, so a recording edited by hand cannot smuggle a value in. A recording is written with O_EXCL and mode 0600; the
owner reviews it before it is copied into the repository.

    python3 tools/live_recording.py kinds
    python3 tools/live_recording.py import --step D5 --kind pod --out DIR FILE
    python3 tools/live_recording.py validate FILE...

Exit status: 0 passed, 1 a problem was found, 2 a usage or environment error. Prints only fixed words, counts and the step
and kind of a recording; never a payload. Standard library only.
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

FORMAT = "factory-runbook-recording/1"
KEYS = ("format", "step", "kind", "recorded_at", "tool", "argv", "returncode", "redactions", "payload", "sha256")
MAX_PAYLOAD_BYTES = 1 << 20
MAX_ARGV = 40
MAX_ARGUMENT = 600  # the --query of `capture --kind az-aks` is 382 characters

# kind -> (form, what the owner may replace in the tests with it). The form is a check on the payload, not on its meaning.
KINDS = {
    "az-quota": ("json", "vCPU usage and limits (`az vm list-usage`)"),
    "az-aks": ("json", "the cluster's profile (`az aks show --query`)"),
    "az-role": ("json", "a role definition (`az role definition list`)"),
    "nodes": ("json", "a node list: sizes, allocatable, labels, taints"),
    "pod": ("json", "a pod object: phase, conditions, container states, the reason a pod is Pending or was evicted"),
    "pod-events": ("json", "the events of one pod (FailedScheduling at a NodePool limit, Evicted, Killing)"),
    "nodeclaim": ("json", "the NodeClaims of node auto-provisioning (the disruption and termination shapes)"),
    "nodepool": ("json", "a NodePool as the API server stored and defaulted it"),
    "crd": ("json", "a custom resource definition (the field names the Terraform writes)"),
    "status-footprint": ("json", "the `footprint` block of `status --json` (sizing of the resource classes)"),
    "kube-status": ("either", "an API error answer (404, 403, 401, 409, 422) as the launcher's client meets it"),
    "denial": ("text", "the first line of an admission denial (the policy name and the bracketed rule ids)"),
    "launcher-reply": ("json", "an answer of the launcher over its socket"),
    "canary": ("text", "the fixed output of the worker wrapper's network canary"),
    "claude-result": ("text", "the `result` event or the tail of a session log for a spend-limit, 401 or 429 failure"),
    "plan-actions": ("text", "the action list of a plan (`<actions> <address>` lines)"),
    "probe-output": ("text", "the output of tools/live_pod_probe.py inside a pod"),
    "timing": ("json", "a measured duration with what was timed (cold start, Cilium downtime, clone, bundle, delta)"),
    "note": ("text", "a number read by a person (the Console tier, a price) and where it was read"),
}

STEP = re.compile(r"[A-G][0-9]{1,2}")
TOOL = re.compile(r"[a-z][a-z0-9_.-]*(?: [a-z][a-z0-9-]*)?")
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
HEX64 = re.compile(r"[0-9a-f]{64}")
UNSAFE = re.compile(r"[^A-Za-z0-9 ._:'/,()\[\]=-]")

ZERO_GUID = "00000000-0000-0000-0000-000000000000"
KEEP_ADDRESSES = ("169.254.169.254", "168.63.129.16")
REDACTED = "<redacted>"

GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
IPV6 = re.compile(
    r"(?<![0-9A-Za-z:])(?=[0-9A-Fa-f:]*[0-9A-Fa-f])"
    r"(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?)(?![0-9A-Za-z:])"
)
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.]|-[0-9])")
CREDENTIALS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])sig=[A-Za-z0-9%+/=_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
# An opaque string: 40 or more base64-ish characters with a digit, an upper and a lower case letter (a digest is lower-case hex
# and a path or a resource name has no upper-case letter, so neither is taken for one).
LONG_BLOB = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
# A long lower-case hexadecimal string (an old-format GitHub token is 40 of them) unless it is a digest, which a fixture needs.
LONG_HEX = re.compile(r"(?<!sha256:)(?<!sha512:)(?<![0-9A-Za-z])[0-9a-f]{40,}(?![0-9A-Za-z])")
# The name of an environment entry that may hold a secret, and the end of a JSON key that does.
ENV_NAME = re.compile(r"(?i)(?:key|token|secret|passw(?:or)?d|authorization|credential)")
KEY_NAME = re.compile(r"(?i)(?:^|[-_.])(?:token|secret|passw(?:or)?d|passwd|authorization|credentials?|api[-_]?key|private[-_]?key|access[-_]?key)$|(?:Token|Secret|Password|ApiKey|PrivateKey|AccessKey)$")
DROPPED = ("managedFields",)
# `token: x`, `password=x`, `--token=x`, `"apiKey": "x"` written in a text: the name ends in a word that says secret, the value follows.
SENSITIVE_WORD = r"(?:token|secret|passw(?:or)?d|passwd|credentials?|authorization|api[-_]?key|private[-_]?key|access[-_]?key)"
ASSIGNMENT = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_.-]*" + SENSITIVE_WORD + r"[\"']?[ \t]*[:=][ \t]*)((?:(?:Basic|Digest|Negotiate)[ \t]+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"']+))")
# A flag of an argument list whose NEXT word is the secret (`["--token", "abc"]`).
FLAG_NAME = re.compile(r"(?i)--?(?:[A-Za-z0-9][A-Za-z0-9_.-]*[-_.])?" + SENSITIVE_WORD)
SECRET_KINDS = ("Secret", "SecretList")
# A Secret written inside a string (the last-applied-configuration annotation is one, as JSON; a describe or a YAML is the other).
SECRET_TEXT = re.compile(r"[\"']?\bkind[\"']?\s*:\s*[\"']?Secret(?:List)?\b")


class Refused(Exception):
    """The input cannot be recorded; `code` is a fixed word."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(payload):
    return hashlib.sha256(canonical(payload).encode("ascii")).hexdigest()


def sanitise(text, limit=120):
    """A fixed-width, printable rendering of text that came from outside (for a fixed-format line)."""
    return UNSAFE.sub("?", str(text))[:limit]


def _is_blob(text):
    return any(c.isdigit() for c in text) and any(c.isupper() for c in text) and any(c.islower() for c in text)


def scrub_text(text):
    """(the text, the number of replacements): credential shapes, GUIDs, IPv4 addresses and long opaque strings."""
    count = 0
    for pattern in CREDENTIALS:
        text, hits = pattern.subn(REDACTED, text)
        count += hits

    def guid(match):
        nonlocal count
        if match.group(0) == ZERO_GUID:
            return ZERO_GUID
        count += 1
        return ZERO_GUID

    def address(match):
        nonlocal count
        if match.group(0) in KEEP_ADDRESSES or match.group(0) == "0.0.0.0":
            return match.group(0)
        count += 1
        return "0.0.0.0"

    def assignment(match):
        nonlocal count
        if match.group(2).strip("\"'").startswith(REDACTED):
            return match.group(0)
        count += 1
        return match.group(1) + REDACTED

    def ipv6(match):
        nonlocal count
        count += 1
        return "::"

    def blob(match):
        nonlocal count
        if not _is_blob(match.group(0)):
            return match.group(0)
        count += 1
        return REDACTED

    text = ASSIGNMENT.sub(assignment, text)
    text, hits = LONG_HEX.subn(REDACTED, text)
    count += hits
    return LONG_BLOB.sub(blob, IPV6.sub(ipv6, IPV4.sub(address, GUID.sub(guid, text)))), count


def _is_secret(node):
    return isinstance(node, dict) and node.get("kind") in SECRET_KINDS


def scrub(value):
    """(a scrubbed deep copy, the number of changes). A Kubernetes Secret anywhere in it is REFUSED."""
    count = 0

    def walk(node):
        nonlocal count
        if _is_secret(node):
            raise Refused("secret-object")
        if isinstance(node, dict):
            out = {}
            sensitive_entry = isinstance(node.get("name"), str) and ENV_NAME.search(node["name"]) is not None
            for key, item in node.items():
                if key in DROPPED:
                    count += 1
                    continue
                if (sensitive_entry and key == "value" and isinstance(item, str)) or (
                    KEY_NAME.search(str(key)) and isinstance(item, str) and item
                ):
                    if item != REDACTED:
                        count += 1
                    out[key] = REDACTED
                    continue
                out[key] = walk(item)
            return out
        if isinstance(node, list):
            out = []
            for index, item in enumerate(node):
                flag = node[index - 1] if index else None
                if isinstance(flag, str) and FLAG_NAME.fullmatch(flag) and isinstance(item, str) and item and not item.startswith("-"):
                    if item != REDACTED:
                        count += 1
                    out.append(REDACTED)
                    continue
                out.append(walk(item))
            return out
        if isinstance(node, str):
            if SECRET_TEXT.search(node):
                raise Refused("secret-object")
            text, hits = scrub_text(node)
            count += hits
            return text
        return node

    return walk(copy.deepcopy(value)), count


def leaks(value):
    """The number of things `scrub` would still change in an already-scrubbed value (a clean recording has none)."""
    try:
        _, changes = scrub(value)
    except Refused:
        return 1
    return changes


def now_utc(clock=None):
    moment = clock() if clock else datetime.datetime.now(datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _argv_problem(argv):
    if argv is None:
        return None
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV:
        return "argv"
    for item in argv:
        if not isinstance(item, str) or len(item) > MAX_ARGUMENT or leaks(item):
            return "argv"
    if leaks(argv):
        return "argv"
    return None


def make(step, kind, payload, argv=None, returncode=None, tool="live_recording import", clock=None):
    """A recording document for `payload`, scrubbed; Refused with a fixed code when it cannot be recorded."""
    if not STEP.fullmatch(str(step)):
        raise Refused("step")
    if kind not in KINDS:
        raise Refused("kind")
    form = KINDS[kind][0]
    if isinstance(payload, str):
        if form == "json":
            raise Refused("form")
        payload = {"text": payload}
    elif form == "text":
        raise Refused("form")
    if not isinstance(payload, (dict, list)):
        raise Refused("form")
    if _argv_problem(argv):
        raise Refused("argv")
    if returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int)):
        raise Refused("returncode")
    cleaned, count = scrub(payload)
    if len(canonical(cleaned)) > MAX_PAYLOAD_BYTES:
        raise Refused("size")
    return {
        "format": FORMAT,
        "step": step,
        "kind": kind,
        "recorded_at": now_utc(clock),
        "tool": tool,
        "argv": argv,
        "returncode": returncode,
        "redactions": count,
        "payload": cleaned,
        "sha256": digest(cleaned),
    }


def problems(document):
    """The fixed problem words of a recording document (empty when it is a clean recording)."""
    if not isinstance(document, dict):
        return ["document"]
    found = []
    if tuple(sorted(document)) != tuple(sorted(KEYS)):
        return ["keys"]
    if document["format"] != FORMAT:
        found.append("format")
    if not isinstance(document["step"], str) or not STEP.fullmatch(document["step"]):
        found.append("step")
    kind = document["kind"]
    if kind not in KINDS:
        found.append("kind")
    if not isinstance(document["recorded_at"], str) or not TIME.fullmatch(document["recorded_at"]):
        found.append("time")
    if not isinstance(document["tool"], str) or not TOOL.fullmatch(document["tool"]):
        found.append("tool")
    if _argv_problem(document["argv"]):
        found.append("argv")
    code = document["returncode"]
    if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
        found.append("returncode")
    if isinstance(document["redactions"], bool) or not isinstance(document["redactions"], int) or document["redactions"] < 0:
        found.append("redactions")
    payload = document["payload"]
    if not isinstance(payload, (dict, list)):
        found.append("payload")
        return found
    if kind in KINDS:
        is_text = isinstance(payload, dict) and tuple(payload) == ("text",) and isinstance(payload["text"], str)
        if (KINDS[kind][0] == "text" and not is_text) or (KINDS[kind][0] == "json" and is_text):
            found.append("form")
    if len(canonical(payload)) > MAX_PAYLOAD_BYTES:
        found.append("size")
    if document["sha256"] != digest(payload):
        found.append("sha256")
    try:
        if leaks(payload):
            found.append("unscrubbed")
    except RecursionError:
        found.append("size")
    return found


def write(document, directory):
    """Write one recording into an existing directory (O_EXCL, 0600, no symlink); the file name."""
    name = f"{document['step']}-{document['kind']}-{document['sha256'][:10]}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.path.join(directory, name), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return name


def read_bounded(path, limit=MAX_PAYLOAD_BYTES * 2):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused("size")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("encoding") from None


def command_import(args, emit):
    form = KINDS[args.kind][0]
    try:
        text = read_bounded(args.file)
        if form == "text" or (form == "either" and not _looks_json(text)):
            payload = text
        else:
            try:
                payload = json.loads(text)
            except ValueError:
                raise Refused("json") from None
        document = make(args.step, args.kind, payload)
        name = write(document, args.out)
    except Refused as refusal:
        emit(f"FAIL not recorded: {refusal.code}")
        return 1
    except FileExistsError:
        emit("FAIL not recorded: a recording with this content exists")
        return 1
    except OSError:
        emit("FAIL not recorded: a file could not be read or written")
        return 2
    emit(f"recorded {document['step']} {document['kind']}: {document['redactions']} redactions, file {sanitise(name)}")
    return 0


def _looks_json(text):
    return text.lstrip()[:1] in ("{", "[")


def command_validate(args, emit):
    bad = 0
    for index, path in enumerate(args.files, 1):
        try:
            document = json.loads(read_bounded(path, MAX_PAYLOAD_BYTES * 4))
        except (Refused, ValueError, OSError):
            emit(f"FAIL recording {index}: unreadable")
            bad += 1
            continue
        found = problems(document)
        if found:
            emit(f"FAIL recording {index}: {','.join(found)}")
            bad += 1
        else:
            emit(f"ok recording {index}: {document['step']} {document['kind']}")
    emit(f"recordings: {len(args.files) - bad} ok, {bad} problems")
    return 1 if bad else 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Write and check recordings of the live runbook.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("kinds")
    imp = sub.add_parser("import")
    imp.add_argument("--step", required=True)
    imp.add_argument("--kind", required=True, choices=sorted(KINDS))
    imp.add_argument("--out", required=True)
    imp.add_argument("file")
    val = sub.add_parser("validate")
    val.add_argument("files", nargs="+")
    return parser.parse_args(argv)


def main(argv=None, emit=print):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    if args.command == "kinds":
        for kind in sorted(KINDS):
            emit(f"{kind}: {KINDS[kind][0]}")
        return 0
    if args.command == "import":
        if not STEP.fullmatch(args.step) or not Path(args.out).is_dir():
            emit("FAIL the step must be a part letter A to G and a number, and the directory must exist")
            return 2
        return command_import(args, emit)
    return command_validate(args, emit)


if __name__ == "__main__":
    sys.exit(main())
