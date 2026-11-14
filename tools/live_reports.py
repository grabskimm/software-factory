#!/usr/bin/env python3
"""Reports over files the person running the live runbook saved (slice 21 of the pod backend, docs/pod-backend-runbook.md).

These commands call nothing: no Azure, no cluster, no network. They read a file the Runner saved from a read-only call, bounded
in size, and print a fixed verdict with counts and numbers. They exist so that the judgement of a step is not a person's eyes on
a long output:

    footprint FILE...        the `footprint` block of `status --json` (one file per run) against the resource classes
    role-diff FILE           `az role definition list --name ROLE -o json` against infra/roles/factory-plan-cluster-reader.json
    mirror-role              the role definition of the MIRROR's plan identity: another name, the mirror's scope (prints JSON)
    plan-log FILE            a saved `terraform plan` output: no error, no Forbidden, no destroy, no change of the manager Deployment unless allowed, a summary line
    boot-compare BEFORE AFTER   two `kubectl get nodes -o json` files: which nodes restarted (a boot id changed)

Safe by construction: file reads only (bounded to 8 MiB, regular files), no shell, nothing written. `mirror-role` prints the role
definition JSON on purpose (the repository's own file with the name and scope changed); everything else prints fixed words, counts and
numbers, plus the Azure data-action names of the repository's own role file. Exit status: 0 passed, 1 a problem was found, 2 a usage
or environment error. Standard library only (the class sizes come from ``factory.podcontract``, hence PYTHONPATH=src).
"""

import argparse
import json
import re
import sys
from pathlib import Path

import live_recording as rec
from factory import podcontract as pc

ROOT = Path(__file__).resolve().parent.parent
ROLE_FILE = ROOT / "infra" / "roles" / "factory-plan-cluster-reader.json"
MAX_FILE_BYTES = 8 * 1024 * 1024
MIN_INVOCATIONS = 20  # docs/capacity.md, "Procedure": at least 20 sessions per phase
PREFIX = re.compile(r"[a-z][a-z0-9-]{0,10}[a-z0-9]")
GUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
RESOURCE_GROUP = re.compile(r"[A-Za-z0-9._()-]{1,89}[A-Za-z0-9_()-]")
ACTION_NAME = re.compile(r"[A-Za-z0-9./_-]{1,200}")


class Unreadable(Exception):
    """A file that is not a regular, small, UTF-8 file."""


def read_text(path):
    try:
        path = Path(path)
        if not path.is_file():
            raise Unreadable
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise Unreadable
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Unreadable from None


def read_json(path):
    try:
        return json.loads(read_text(path))
    except ValueError:
        raise Unreadable from None


def mib(quantity):
    match = re.fullmatch(r"([0-9]+)(Mi|Gi)", quantity)
    return int(match.group(1)) * (1024 if match.group(2) == "Gi" else 1) if match else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- footprint --------------------------------------------------------------------------------------------------------------


def combine_footprints(documents):
    """Per phase: invocations (sum), peak (max), p95 (the largest p95 of any run: an UPPER BOUND, p95s cannot be combined)."""
    phases = {}
    for document in documents:
        block = document.get("footprint") if isinstance(document, dict) else None
        if not isinstance(block, dict):
            raise Unreadable
        for phase, entry in block.items():
            keys = ("invocations", "peak_rss_mb", "p95_rss_mb", "peak_cpu_seconds", "p95_cpu_seconds")
            if not isinstance(entry, dict) or not all(is_number(entry.get(key)) for key in keys):
                raise Unreadable
            into = phases.setdefault(str(phase), {key: 0 for key in keys})
            into["invocations"] += entry["invocations"]
            for key in keys[1:]:
                into[key] = max(into[key], entry[key])
    return phases


def cmd_footprint(args, emit):
    try:
        phases = combine_footprints([read_json(path) for path in args.files])
    except Unreadable:
        emit("FAIL footprint: a file is not the output of `status --json` with a footprint block")
        return 1
    if not phases:
        emit("FAIL footprint: no phase has a footprint record")
        return 1
    problems = 0
    for phase, entry in sorted(phases.items()):
        emit(
            f"phase {rec.sanitise(phase, 20)}: invocations {entry['invocations']}, peak rss {entry['peak_rss_mb']:.1f} MiB, "
            f"p95 rss (upper bound) {entry['p95_rss_mb']:.1f} MiB, peak cpu {entry['peak_cpu_seconds']:.1f} s, "
            f"p95 cpu (upper bound) {entry['p95_cpu_seconds']:.1f} s"
        )
        if entry["invocations"] < MIN_INVOCATIONS:
            emit(f"note: phase {rec.sanitise(phase, 20)} has fewer than {MIN_INVOCATIONS} invocations: not a measurement")
        fitting = []
        for name, spec in sorted(pc.CLASSES.items()):
            limit = mib(spec["memory"])
            fits = limit is not None and entry["peak_rss_mb"] < limit
            fitting.append(fits)
            emit(f"phase {rec.sanitise(phase, 20)} class {name}: peak rss {'below' if fits else 'NOT below'} the memory limit of {limit} MiB")
        if not any(fitting):
            emit(f"FAIL phase {rec.sanitise(phase, 20)}: no resource class has a memory limit above the peak")
            problems += 1
    emit("footprint: every phase fits at least one class" if not problems else f"FAIL footprint: {problems} phases fit no class")
    return 1 if problems else 0


# --- role -------------------------------------------------------------------------------------------------------------------


def live_data_actions(document):
    """The data actions of `az role definition list -o json` output (a list of one definition, or one definition)."""
    definition = document[0] if isinstance(document, list) and len(document) == 1 else document
    if not isinstance(definition, dict):
        raise Unreadable
    actions = []
    for permission in definition.get("permissions") or ():
        found = permission.get("dataActions") if isinstance(permission, dict) else None
        if not isinstance(found, list) or not all(isinstance(a, str) for a in found):
            raise Unreadable
        actions.extend(found)
    return definition, actions


def cmd_role_diff(args, emit):
    try:
        wanted = [a for a in read_json(ROLE_FILE)["DataActions"]]
        definition, live = live_data_actions(read_json(args.file))
    except (Unreadable, KeyError, TypeError):
        emit("FAIL role-diff: the file is not the output of `az role definition list` for one role")
        return 1
    emit(f"repository role: {len(wanted)} data actions; live role: {len(live)} data actions")
    missing, extra = sorted(set(wanted) - set(live)), sorted(set(live) - set(wanted))
    for action in missing:
        emit(f"missing in the live role: {rec.sanitise(action, 200) if ACTION_NAME.fullmatch(action) else '<unprintable>'}")
    for action in extra:
        emit(f"extra in the live role: {rec.sanitise(action, 200) if ACTION_NAME.fullmatch(action) else '<unprintable>'}")
    if definition.get("roleName") == json.loads(read_text(ROLE_FILE))["Name"]:
        emit("role name: the production name")
    problems = len(missing) + len(extra)
    emit("role-diff: the live role equals the repository file" if not problems else f"FAIL role-diff: {problems} differences")
    return 1 if problems else 0


def mirror_role(prefix, subscription, resource_group):
    """The repository's role definition with a name that cannot be the production role's and the mirror's resource group as scope."""
    base = json.loads(read_text(ROLE_FILE))
    role = dict(base)
    role["Name"] = f"{base['Name']} {prefix}"
    role["Description"] = f"MIRROR of the plan identity's role ({prefix}). " + base["Description"]
    role["AssignableScopes"] = [f"/subscriptions/{subscription}/resourceGroups/{resource_group}"]
    return role


def cmd_mirror_role(args, emit):
    if not PREFIX.fullmatch(args.prefix) or "--" in args.prefix:
        emit("FAIL mirror-role: the prefix is not a name_prefix (2 to 12 lowercase letters, digits and single hyphens)")
        return 2
    if not GUID_TEXT.fullmatch(args.subscription) or not RESOURCE_GROUP.fullmatch(args.resource_group):
        emit("FAIL mirror-role: the subscription must be a GUID and the resource group a resource group name")
        return 2
    emit(json.dumps(mirror_role(args.prefix, args.subscription, args.resource_group), indent=2))
    return 0


# --- terraform plan ---------------------------------------------------------------------------------------------------------

SUMMARY = re.compile(r"^Plan: (\d+) to add, (\d+) to change, (\d+) to destroy\.$", re.MULTILINE)
NO_CHANGES = re.compile(r"^No changes\.", re.MULTILINE)
ERROR_LINE = re.compile(r"^(?:╷\s*)?(?:│\s*)?Error: ", re.MULTILINE)
DESTROY_LINE = re.compile(r"^\s*# \S+ (?:will be destroyed|must be replaced)", re.MULTILINE)
DEPLOYMENT_LINE = re.compile(r"^\s*# kubernetes_manifest\.factory_deployment(?:\[[^\]]*\])? (?:will be (?:updated in-place|destroyed)|must be replaced)", re.MULTILINE)
CLUSTER_LINE = re.compile(r"^\s*# azurerm_kubernetes_cluster(?:_node_pool)?\.\S+ (?:will be (?:updated in-place|destroyed)|must be replaced)", re.MULTILINE)


def cmd_plan_log(args, emit):
    try:
        text = read_text(args.file)
    except Unreadable:
        emit("FAIL plan-log: the file could not be read")
        return 1
    errors, forbidden = len(ERROR_LINE.findall(text)), len(re.findall(r"(?i)forbidden", text))
    destroys, deployment, cluster = len(DESTROY_LINE.findall(text)), len(DEPLOYMENT_LINE.findall(text)), len(CLUSTER_LINE.findall(text))
    summary = SUMMARY.search(text)
    problems = 0
    if summary:
        emit(f"plan summary: {summary.group(1)} to add, {summary.group(2)} to change, {summary.group(3)} to destroy")
    elif NO_CHANGES.search(text):
        emit("plan summary: no changes")
    else:
        emit("FAIL plan-log: no plan summary line (the plan did not finish)")
        problems += 1
    emit(f"errors: {errors}, forbidden mentions: {forbidden}, destroy or replace lines: {destroys}")
    for count, text_ in ((errors, "the plan has an error"), (forbidden, "the plan was refused something (Forbidden)"), (destroys, "the plan destroys or replaces an object")):
        if count:
            emit(f"FAIL plan-log: {text_}")
            problems += 1
    if deployment and not args.allow_deployment:
        emit("FAIL plan-log: the plan changes the manager Deployment (a restart on its ReadWriteOnce disk; pass --allow-deployment for the step that does)")
        problems += 1
    elif deployment:
        emit(f"note: the plan changes the manager Deployment ({deployment} lines), which this step allows")
    if cluster and not args.allow_cluster:
        emit("FAIL plan-log: the plan changes the cluster or a node pool (pass --allow-cluster for the step that does)")
        problems += 1
    if args.expect_no_changes and summary:
        emit("FAIL plan-log: changes were planned where none are expected")
        problems += 1
    emit("plan-log: passed" if not problems else f"FAIL plan-log: {problems} problems")
    return 1 if problems else 0


# --- node restarts ----------------------------------------------------------------------------------------------------------


def boot_ids(document):
    found = {}
    for node in document.get("items") or ():
        name = (node.get("metadata") or {}).get("name")
        boot = ((node.get("status") or {}).get("nodeInfo") or {}).get("bootID")
        if not isinstance(name, str) or not isinstance(boot, str) or not boot:
            raise Unreadable
        found[name] = boot
    return found


def cmd_boot_compare(args, emit):
    try:
        before, after = boot_ids(read_json(args.before)), boot_ids(read_json(args.after))
    except (Unreadable, AttributeError, TypeError):
        emit("FAIL boot-compare: a file is not `kubectl get nodes -o json` output with a boot id for every node")
        return 1
    same = sum(1 for name in before if after.get(name) == before[name])
    restarted = sum(1 for name in before if name in after and after[name] != before[name])
    gone = sum(1 for name in before if name not in after)
    new = sum(1 for name in after if name not in before)
    emit(f"nodes before: {len(before)}, after: {len(after)}; same boot: {same}, restarted: {restarted}, gone: {gone}, new: {new}")
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Reports over files saved from the live runbook.")
    sub = parser.add_subparsers(dest="command", required=True)
    foot = sub.add_parser("footprint")
    foot.add_argument("files", nargs="+")
    role = sub.add_parser("role-diff")
    role.add_argument("file")
    mirror = sub.add_parser("mirror-role")
    mirror.add_argument("--prefix", required=True)
    mirror.add_argument("--subscription", required=True)
    mirror.add_argument("--resource-group", required=True)
    plan = sub.add_parser("plan-log")
    plan.add_argument("file")
    plan.add_argument("--expect-no-changes", action="store_true")
    plan.add_argument("--allow-cluster", action="store_true")
    plan.add_argument("--allow-deployment", action="store_true", help="the step that turns the pod backend on changes the manager Deployment (slice 17)")
    boot = sub.add_parser("boot-compare")
    boot.add_argument("before")
    boot.add_argument("after")
    return parser.parse_args(argv)


COMMANDS = {
    "footprint": cmd_footprint,
    "role-diff": cmd_role_diff,
    "mirror-role": cmd_mirror_role,
    "plan-log": cmd_plan_log,
    "boot-compare": cmd_boot_compare,
}


def main(argv=None, emit=print):
    args = parse_arguments(sys.argv[1:] if argv is None else argv)
    return COMMANDS[args.command](args, emit)


if __name__ == "__main__":
    sys.exit(main())
