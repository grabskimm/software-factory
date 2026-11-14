#!/usr/bin/env bash
# Smoke test for the Factory image. One PASS or FAIL line per check; exit status 1
# if any check failed.
#
#   bash image/smoke.sh IMAGE
#
# IMAGE is any reference `docker run` accepts (a tag, or repo@sha256:...). Needs
# docker. Checks are about the image contents and the entrypoint's privilege model;
# whether bubblewrap can create user namespaces depends on the container runtime
# (spike S1) and is checked only when SMOKE_SANDBOX=1 (see the end of this file).
# SC2016: the single-quoted scripts passed to `sh -c` run inside the container, so
# the variables in them must NOT be expanded by this shell.
# shellcheck disable=SC2016
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
	echo "usage: bash image/smoke.sh IMAGE" >&2
	exit 2
fi
image=$1
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/.." && pwd)

failures=0
pass() { printf 'PASS %s\n' "$1"; }
fail() {
	printf 'FAIL %s: %s\n' "$1" "$2"
	failures=$((failures + 1))
}
# one line, at most 200 characters, for the FAIL detail
oneline() { tr '\n' ' ' | cut -c1-200; }

# check NAME COMMAND [ARG...]: passes when the command exits 0
check() {
	local name=$1 out
	shift
	if out=$("$@" 2>&1); then
		pass "$name"
	else
		fail "$name" "$(printf '%s' "$out" | oneline)"
	fi
}

# expect_eq NAME EXPECTED ACTUAL
expect_eq() {
	if [ "$2" = "$3" ]; then
		pass "$1"
	else
		fail "$1" "expected '$2', got '$3'"
	fi
}

# run COMMAND [ARG...]: in a fresh container, through the real entrypoint
run() { docker run --rm "$image" "$@"; }

# as_worker COMMAND [ARG...]: the supervisor's way of starting a worker,
# subprocess.Popen(user=, group=, extra_groups=[]) behind the capability-dropping
# wrapper (factory.agent.drop_capabilities_first), from inside the container
worker_py='
import os, subprocess, sys
from factory.agent import drop_capabilities_first
env = dict(os.environ, HOME="/tmp")
done = subprocess.run(drop_capabilities_first(sys.argv[1:]), cwd="/tmp", user=10002, group=10010, extra_groups=[], env=env, capture_output=True, text=True)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
'
as_worker() { docker run --rm "$image" /opt/factory/bin/python -c "$worker_py" "$@"; }

# pinned versions: Claude Code from the lockfile's package.json, gh from the Dockerfile
claude_want=$(sed -n 's/.*"@anthropic-ai\/claude-code": *"\([^"]*\)".*/\1/p' "$here/claude/package.json")
gh_want=${GH_VERSION:-$(sed -n 's/^ARG GH_VERSION=//p' "$repo/Dockerfile")}
terraform_want=$(sed -n 's/^ARG TERRAFORM_VERSION=//p' "$repo/Dockerfile")
if [ -z "$claude_want" ] || [ -z "$gh_want" ] || [ -z "$terraform_want" ]; then
	echo "cannot read the pinned versions from image/claude/package.json and the Dockerfile" >&2
	exit 2
fi

# --- identity and capabilities ------------------------------------------------
out=$(run id -u 2>/dev/null) || out="command failed"
expect_eq "entrypoint: id -u is 10001" "10001" "$out"

out=$(run id -G 2>/dev/null) || out="command failed"
expect_eq "entrypoint: primary group factory (10001), supplementary factory-work (10010)" "10001 10010" "$out"

want_caps='CapInh:00000000000000e0 CapPrm:00000000000000e0 CapEff:00000000000000e0 CapBnd:00000000000000e0 CapAmb:00000000000000e0 '
out=$(run grep -E '^Cap(Inh|Prm|Eff|Bnd|Amb):' /proc/self/status 2>/dev/null | awk '{printf "%s%s ", $1, $2}') || out="command failed"
expect_eq "entrypoint: capability sets are exactly setuid+setgid+kill (CapAmb 00000000000000e0)" "$want_caps" "$out"

out=$(run grep -E '^NoNewPrivs:' /proc/self/status 2>/dev/null | awk '{print $2}') || out="command failed"
expect_eq "entrypoint: no_new_privs is set" "1" "$out"

out=$(docker run --rm --user 10001:10010 --cap-drop ALL "$image" id -u 2>/dev/null) || out="command failed"
expect_eq "entrypoint: an already unprivileged container just execs the command" "10001" "$out"

# --- tools --------------------------------------------------------------------
out=$(run claude --version 2>/dev/null) || out="command failed"
expect_eq "claude --version equals the pinned version" "$claude_want (Claude Code)" "$out"

out=$(as_worker /usr/local/bin/claude --version 2>/dev/null) || out="command failed"
expect_eq "worker: Popen(user=worker) behind the wrapper runs claude --version" "$claude_want (Claude Code)" "$out"

out=$(as_worker grep -E '^Cap(Prm|Eff|Amb):' /proc/self/status 2>/dev/null | awk '{printf "%s%s ", $1, $2}') || out="command failed"
expect_eq "worker: no permitted, effective or ambient capabilities" "CapPrm:0000000000000000 CapEff:0000000000000000 CapAmb:0000000000000000 " "$out"

out=$(as_worker /opt/factory/bin/python -c 'import os
for call, value in ((os.setuid, 10001), (os.setuid, 0), (os.setgid, 0)):
    try:
        call(value)
        print("SUCCEEDED")
        break
    except PermissionError:
        pass
else:
    print("refused")' 2>/dev/null) || out="command failed"
expect_eq "worker: setuid to the supervisor or root, and setgid to root, are refused" "refused" "$out"

out=$(run gh --version 2>/dev/null | head -n 1) || out="command failed"
case "$out" in
"gh version $gh_want "*) pass "gh --version is $gh_want" ;;
*) fail "gh --version is $gh_want" "got '$out'" ;;
esac

check "git --version" run git --version
check "bwrap --version" run bwrap --version
check "socat -V" run socat -V
check "tini --version" run tini --version

out=$(run stat -c %a /usr/bin/bwrap 2>/dev/null) || out="command failed"
expect_eq "bwrap is not setuid (setuid bwrap would not work with no_new_privs)" "755" "$out"

# --- verification tools (tier 1, docs/verification-tiers.md) --------------------
# Small, pinned, offline tools that need no privilege and no daemon. Each one runs as
# the worker (uid 10002, no capabilities) because that is who uses it. The inputs are
# written under mktemp -d, so nothing here needs the network or a writable HOME.
check "worker: jq reads JSON and -e turns a false result into an exit status" \
	as_worker sh -c 'echo "{\"n\": 3}" | jq -e ".n == 3" >/dev/null && ! echo "{\"n\": 3}" | jq -e ".n == 4" >/dev/null'

check "worker: shellcheck accepts a quoted expansion and rejects an unquoted one" \
	as_worker sh -c 'd=$(mktemp -d) && printf "#!/bin/sh\necho \"\$1\"\n" >"$d/ok.sh" && printf "#!/bin/sh\necho \$1\n" >"$d/bad.sh" && shellcheck "$d/ok.sh" && ! shellcheck "$d/bad.sh" >/dev/null'

check "worker: make runs a rule from a Makefile" \
	as_worker sh -c 'd=$(mktemp -d) && printf "all:\n\t@echo built\n" >"$d/Makefile" && [ "$(make -s -C "$d")" = built ]'

out=$(as_worker /usr/local/bin/terraform version 2>/dev/null | sed -n 1p) || out="command failed"
expect_eq "worker: terraform version equals the pinned version" "Terraform v$terraform_want" "$out"

check "worker: terraform fmt -check accepts formatted and rejects unformatted configuration" \
	as_worker sh -c 'd=$(mktemp -d) && printf "variable \"a\" {\n  type = string\n}\n" >"$d/ok.tf" && printf "variable \"a\" {\ntype=string\n}\n" >"$d/bad.tf" && terraform fmt -check "$d/ok.tf" && ! terraform fmt -check "$d/bad.tf" >/dev/null'

check "terraform cannot call home: CHECKPOINT_DISABLE=1 is set" \
	run sh -c '[ "$CHECKPOINT_DISABLE" = 1 ]'

# Broad tools, not broad privileges: nothing that needs a daemon, root or a
# vulnerability database goes into the supervisor image (image/README.md).
check "no docker, dockerd or trivy in the image" \
	run sh -c '! command -v docker && ! command -v dockerd && ! command -v trivy'

# --- users --------------------------------------------------------------------
out=$(run id worker 2>/dev/null) || out="command failed"
case "$out" in
"uid=10002(worker) gid=10010(factory-work) groups=10010(factory-work)") pass "id worker: uid 10002, gid 10010 (factory-work)" ;;
*) fail "id worker: uid 10002, gid 10010 (factory-work)" "got '$out'" ;;
esac

out=$(docker run --rm --entrypoint getent "$image" passwd worker 2>/dev/null | cut -d: -f7) || out="command failed"
expect_eq "worker: no login shell" "/usr/sbin/nologin" "$out"

out=$(docker run --rm --entrypoint getent "$image" shadow worker 2>/dev/null | cut -d: -f2 | cut -c1) || out="command failed"
case "$out" in
'!' | '*') pass "worker: no password" ;;
*) fail "worker: no password" "shadow password field starts with '$out'" ;;
esac

check "worker cannot read /home/factory" as_worker sh -c 'test -d /home/factory && ! test -r /home/factory && ! ls /home/factory'

check "code is root-owned and not writable by factory" \
	run sh -c '[ "$(stat -c %U /opt/factory)" = root ] && [ "$(stat -c %U /opt/token-broker)" = root ] && [ ! -w /opt/factory ] && [ ! -w /opt/token-broker ] && [ ! -w /etc/claude-code/managed-settings.json ]'

# COPY --chmod must not leak into parent directories (BuildKit >= 0.23 did that).
for dir in /etc/claude-code /opt/factory-tools /opt/factory-spikes; do
	out=$(run stat -c '%a %U:%G' "$dir" 2>/dev/null) || out="command failed"
	expect_eq "$dir is mode 755 and owned by root" "755 root:root" "$out"
done
out=$(run stat -c %a /etc/claude-code/managed-settings.json /opt/factory-tools/sandbox_e2e.py 2>/dev/null | tr '\n' ' ') || out="command failed"
expect_eq "managed-settings.json and sandbox_e2e.py are mode 644" "644 644 " "$out"
check "worker (uid 10002) can read managed-settings.json and sandbox_e2e.py" \
	as_worker sh -c 'cat /etc/claude-code/managed-settings.json /opt/factory-tools/sandbox_e2e.py >/dev/null'

# --- factory package ----------------------------------------------------------
# The subcommand list is the {a,b,c} choice list in the usage line, not prose.
no_merge_close() { # NAME COMMAND...
	local name=$1 list bad
	shift
	list=$(run "$@" 2>/dev/null | grep -o '{[a-z,-]*}' | head -n 1 | tr -d '{}' | tr ',' '\n') || list=""
	if ! printf '%s\n' "$list" | grep -qx 'plan'; then
		fail "$name" "no subcommand list found in --help"
		return
	fi
	bad=$(printf '%s\n' "$list" | grep -E 'merge|close' || true)
	if [ -n "$bad" ]; then
		fail "$name" "forbidden subcommand(s): $(printf '%s' "$bad" | oneline)"
	else
		pass "$name"
	fi
}
no_merge_close "factory --help lists no merge or close commands" factory --help
no_merge_close "python -m factory --help lists no merge or close commands" /opt/factory/bin/python -m factory --help

check "import factory.manager" run /opt/factory/bin/python -c 'import factory.manager'
# The dashboard page (src/factory/ui, served at /ui/) is package data: it must be in the installed package.
check "dashboard page files are installed" \
	run /opt/factory/bin/python -c 'from factory import webui; assert webui.load() is not None'

# --- configuration baked into the image -----------------------------------------
check "managed-settings.json exists and is valid JSON" \
	run /opt/factory/bin/python -c 'import json; json.load(open("/etc/claude-code/managed-settings.json"))'

if [ -f "$repo/managed-settings.json" ]; then
	if diff <(docker run --rm "$image" cat /etc/claude-code/managed-settings.json 2>/dev/null) "$repo/managed-settings.json" >/dev/null; then
		pass "managed-settings.json in the image equals the repository file"
	else
		fail "managed-settings.json in the image equals the repository file" "files differ"
	fi
fi

check "token broker: venv has cryptography and FACTORY_TOKEN_BROKER paths exist" \
	run sh -c 'set -- $FACTORY_TOKEN_BROKER; [ -x "$1" ] && [ -f "$2" ] && "$1" -c "import cryptography"'

check "FACTORY_CONFIG and FACTORY_DATA are set and usable; no FACTORY_AGENT_CMD override" \
	run sh -c '[ "$FACTORY_CONFIG" = /config ] && [ "$FACTORY_DATA" = /data ] && [ -d /config ] && [ -w /data/repos ] && [ -z "${FACTORY_AGENT_CMD:-}" ]'

check "a worker identity is required: run_agent refuses a call without [worker] uid/gid" \
	run sh -c '[ "$FACTORY_REQUIRE_WORKER_IDENTITY" = 1 ] && /opt/factory/bin/python -c "
import sys, tempfile
from factory.agent import AgentError, run_agent
try:
    run_agent(\"p\", tempfile.gettempdir(), tempfile.gettempdir() + \"/x/0-plan\", \"plan\")
except AgentError as error:
    sys.exit(0 if \"[worker] uid and gid\" in str(error) else 1)
sys.exit(1)
"'

check "Claude Code switches: autoupdater, telemetry and nonessential traffic off" \
	run sh -c '[ "$DISABLE_AUTOUPDATER" = 1 ] && [ "$DISABLE_TELEMETRY" = 1 ] && [ "$CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" = 1 ]'

# Present only when the image was built with TOOLCHAINS="node@22".
out=$(run sh -c 'if [ -x /opt/toolchains/node/bin/node ]; then /opt/toolchains/node/bin/node --version; else echo none; fi' 2>/dev/null) || out="command failed"
case "$out" in
"command failed") fail "toolchains: /opt/toolchains" "$out" ;;
none) pass "toolchains: none built (TOOLCHAINS was empty)" ;;
*) pass "toolchains: node $out" ;;
esac

# With the node toolchain: node and corepack run as the worker user, npm/npx are gone
# (their dependency tree carries HIGH vulnerability findings).
if [ "$out" != none ] && [ "$out" != "command failed" ]; then
	out=$(as_worker /opt/toolchains/node/bin/node --version 2>/dev/null) || out="command failed"
	case "$out" in
	v22.*) pass "worker: toolchain node --version ($out)" ;;
	*) fail "worker: toolchain node --version" "got '$out'" ;;
	esac
	out=$(as_worker /opt/toolchains/node/bin/corepack --version 2>/dev/null) || out="command failed"
	case "$out" in
	[0-9]*.[0-9]*.[0-9]*) pass "worker: toolchain corepack --version ($out)" ;;
	*) fail "worker: toolchain corepack --version" "got '$out'" ;;
	esac
	check "toolchains: npm and npx are absent from /opt/toolchains/node" \
		run sh -c '[ ! -e /opt/toolchains/node/bin/npm ] && [ ! -L /opt/toolchains/node/bin/npm ] && [ ! -e /opt/toolchains/node/bin/npx ] && [ ! -L /opt/toolchains/node/bin/npx ] && [ ! -e /opt/toolchains/node/lib/node_modules/npm ]'
fi

# --- optional: the real sandbox ---------------------------------------------------
# Docker's default seccomp and AppArmor profiles block user namespaces, so this
# only runs on request and then without them. It is NOT a substitute for spike S1,
# which has to be run on the target cluster itself.
if [ "${SMOKE_SANDBOX:-0}" = 1 ]; then
	check "sandbox: factory-sandbox-check (tools/sandbox_e2e.py as the worker)" \
		docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
		"$image" factory-sandbox-check
fi

if [ "$failures" -ne 0 ]; then
	printf '%s check(s) failed\n' "$failures"
	exit 1
fi
echo "all checks passed"
