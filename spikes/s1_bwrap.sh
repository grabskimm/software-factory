#!/usr/bin/env bash
# Spike S1 (part 1): can bubblewrap create the namespaces Claude Code's sandbox needs?
#
# Run it inside the Factory container on ACA (Dedicated workload profile), as root (an
# `az containerapp exec` shell) or as the `factory` user. It uses bwrap directly, the
# lowest layer. Part 2 (tools/sandbox_e2e.py, command `factory-sandbox-check`) checks the
# same thing through the real `claude` CLI with `sandbox.failIfUnavailable`.
#
# bubblewrap refuses to start in a process that holds capabilities ("Unexpected
# capabilities but not setuid"), and both root and the `factory` identity (ambient
# CAP_SETUID/CAP_SETGID/CAP_KILL) do. A worker holds none, and a worker is what the
# sandbox is for, so the script first re-executes itself as the worker identity
# (uid 10002, gid 10010, no capabilities, no_new_privs), exactly like
# `factory-sandbox-check`.
#
#   bash s1_bwrap.sh            # exit 0 = every check passed
set -uo pipefail

if [ -z "${S1_AS_WORKER:-}" ] \
  && { [ "$(id -u)" -eq 0 ] || [ "$(awk '/^CapPrm:/{print $2}' /proc/self/status)" != 0000000000000000 ]; }; then
  S1_AS_WORKER=1 HOME=/tmp exec /usr/bin/setpriv \
    --reuid=10002 --regid=10010 --clear-groups \
    --inh-caps=-all --ambient-caps=-all --no-new-privs \
    -- /usr/bin/env bash "$0" "$@"
fi

fail=0
report() { # report PASS|FAIL "text"
  printf '%s %s\n' "$1" "$2"
  [ "$1" = PASS ] || fail=1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo "id: $(id)"
echo "kernel: $(uname -r)"
echo "bwrap: $(bwrap --version 2>&1 | head -1)"
echo "unprivileged_userns_clone: $(cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null || echo n/a)"
echo "max_user_namespaces: $(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo n/a)"
grep -E '^(Seccomp|NoNewPrivs|CapEff|CapBnd)' /proc/self/status | tr '\t' ' '

base=(bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-uts
  --ro-bind / / --dev /dev --proc /proc --bind "$work" "$work" --chdir "$work")

# 1. the sandbox can start at all
if out="$("${base[@]}" true 2>&1)"; then
  report PASS "bwrap starts with user, pid, ipc and uts namespaces"
else
  report FAIL "bwrap cannot start: ${out}"
  case "$out" in
    *"Unexpected capabilities"*)
      echo "S1 INCONCLUSIVE: this process still holds capabilities, which is not what a worker is."
      echo "Run the script as the worker identity (see the header of this file); this is not an S1 result."
      exit 2
      ;;
  esac
  echo "S1 FAIL: stop here and do not weaken the sandbox (spikes/README.md, 'If a spike FAILs')."
  exit 1
fi

# 2. writes: allowed in the working tree, denied elsewhere
if "${base[@]}" sh -c "echo ok > '$work/inside'" 2>/dev/null && [ -s "$work/inside" ]; then
  report PASS "write inside the bound working directory works"
else
  report FAIL "write inside the bound working directory failed"
fi
if "${base[@]}" sh -c 'touch /etc/s1-outside 2>/dev/null || touch /usr/s1-outside 2>/dev/null'; then
  report FAIL "write outside the working directory succeeded"
else
  report PASS "write outside the working directory is denied"
fi

# 3. another process's environment is not visible from inside (new pid namespace)
secret="s1-canary-$$"
env S1_CANARY="$secret" sleep 120 &
canary_pid=$!
trap 'kill "$canary_pid" 2>/dev/null; rm -rf "$work"' EXIT
if "${base[@]}" sh -c 'cat /proc/[0-9]*/environ 2>/dev/null | tr "\0" "\n" | grep -q "^S1_CANARY="'; then
  report FAIL "environment of an outside process is visible inside the sandbox"
else
  report PASS "no outside process environment is visible inside the sandbox"
fi
kill "$canary_pid" 2>/dev/null || true

# 4. network: with --unshare-net there is no route out, including to the internet
net=("${base[@]}" --unshare-net)
if out="$("${net[@]}" true 2>&1)"; then
  report PASS "bwrap starts with --unshare-net"
  if "${net[@]}" sh -c 'curl -sS -m 5 -o /dev/null https://example.com' 2>/dev/null; then
    report FAIL "curl reached the internet from a network-less sandbox"
  else
    report PASS "curl to a non-allowlisted host fails from a network-less sandbox"
  fi
else
  report FAIL "bwrap cannot start with --unshare-net: ${out}"
fi

# 5. socat is present (Claude Code's network proxy bridge)
if command -v socat >/dev/null 2>&1; then
  report PASS "socat is installed"
else
  report FAIL "socat is missing"
fi

if [ "$fail" -eq 0 ]; then
  echo "S1 part 1: PASS"
else
  echo "S1 part 1: FAIL"
fi
exit "$fail"
