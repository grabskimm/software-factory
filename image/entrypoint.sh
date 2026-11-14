#!/bin/bash
# Factory container entrypoint. Installed as /usr/local/bin/factory-entrypoint
# and started by tini:   ENTRYPOINT ["tini", "--", "/usr/local/bin/factory-entrypoint"]
#
# The container starts as root so that this script can prepare the data tree.
# It then drops to the `factory` identity (uid 10001, primary group factory 10001,
# supplementary group factory-work 10010) and keeps exactly three capabilities as
# AMBIENT capabilities:
#   * CAP_SETUID and CAP_SETGID, to start each worker as the separate `worker` uid
#     (Popen(user=, group=, extra_groups=[])), so that a prompt-injected worker
#     cannot read the supervisor's /proc/<pid>/environ (the GitHub App key, the
#     installation token);
#   * CAP_KILL, because kill(2) between two different UIDs needs it: without it
#     the supervisor cannot signal a worker's process group (lease loss, timeout,
#     drain, purge), see invariant 11. Found with spikes/s3_uid_split.py (EPERM).
#
#   inheritable set = ambient set = bounding set = { setuid, setgid, kill }
#   permitted/effective = the same (the kernel derives them from the ambient set
#   on exec, so the dropped process keeps them)
#
# Bit values: CAP_KILL 5, CAP_SETGID 6, CAP_SETUID 7, so the mask is 0xe0 and a
# process started through this script shows CapAmb: 00000000000000e0.
#
# Why no_new_privs is set (setpriv --no-new-privs): it stops the supervisor and
# every worker from gaining privilege through a setuid or file-capability
# binary (su, mount, ...) and costs nothing here. It does not touch ambient
# capabilities and it does not stop bubblewrap: bubblewrap creates an
# unprivileged user namespace and sets no_new_privs itself. Checked locally with
# util-linux 2.39 and bubblewrap 0.9.0: `setpriv --no-new-privs ... python3
# Popen(user=10002) -> bwrap --unshare-user --unshare-pid` works.
#
# What the WORKER gets is different, and important. The kernel clears capabilities
# on a UID change only to or from 0, so a worker started by Popen(user=) from this
# supervisor would still hold all three (and its ambient set survives exec): it
# could setuid() back to the supervisor and read its environment, and bubblewrap
# refuses to start in a non-root process with permitted capabilities ("Unexpected
# capabilities but not setuid"), which would stop every run under
# sandbox.failIfUnavailable. factory.agent.run_agent therefore starts every
# separate-identity worker through src/factory/execnocaps.py, which clears the
# ambient set (fail closed) before it execs claude.
#
# If the container already starts unprivileged (docker run --user, a pod
# securityContext) there is nothing to drop: the command is executed as is.
#
# This script prints only fixed messages and paths. It never prints the
# environment, so no secret can reach the console log.
set -euo pipefail

readonly FACTORY_UID=10001
readonly FACTORY_GID=10001
readonly WORK_GID=10010
readonly KEEP_CAPS='-all,+setuid,+setgid,+kill'

say() { printf 'factory-entrypoint: %s\n' "$*" >&2; }
warn() { say "WARNING: $*"; }

if [ "$#" -eq 0 ]; then
	say "ERROR: no command given"
	exit 64
fi

# Already unprivileged: just run the command.
if [ "$EUID" -ne 0 ]; then
	exec "$@"
fi

config_dir=${FACTORY_CONFIG:-/config}
data_dir=${FACTORY_DATA:-/data}

# Best effort, and loud on failure. Azure Files NFS may squash root (spike S2 in
# spikes/README.md); then mkdir/chown/chmod can fail here and the data tree must
# be made writable for uid 10001 on the storage side. The supervisor then fails
# on its own, with a clear error, the first time it writes.
if ! mkdir -p -- "$config_dir"; then
	warn "cannot create $config_dir"
fi
if ! mkdir -p -- "$data_dir/repos"; then
	warn "cannot create $data_dir/repos (NFS root squash? see spike S2)"
else
	# The data root and repos/ must let group factory-work traverse (0710) so that
	# the worker identity can reach the worktrees it is given (permissions.py).
	for path in "$data_dir" "$data_dir/repos"; do
		if ! chown "$FACTORY_UID:$WORK_GID" -- "$path"; then
			warn "cannot chown $path to $FACTORY_UID:$WORK_GID (NFS root squash? see spike S2)"
		fi
		if ! chmod 0710 -- "$path"; then
			warn "cannot chmod 0710 $path"
		fi
	done
	# Chown can "succeed" on a squashed export without giving us a usable tree, so
	# test what matters: can the factory identity write there?
	if ! setpriv --reuid="$FACTORY_UID" --regid="$FACTORY_GID" --groups="$WORK_GID" \
		test -w "$data_dir/repos"; then
		warn "uid $FACTORY_UID cannot write $data_dir/repos; runs will fail until the share allows it (spike S2)"
	fi
fi

# The root environment points HOME at /root; the supervisor and git/gh use HOME.
export HOME=/home/factory USER=factory LOGNAME=factory

exec setpriv \
	--reuid="$FACTORY_UID" --regid="$FACTORY_GID" --groups="$WORK_GID" \
	--inh-caps="$KEEP_CAPS" \
	--ambient-caps="$KEEP_CAPS" \
	--bounding-set="$KEEP_CAPS" \
	--no-new-privs \
	-- "$@"
