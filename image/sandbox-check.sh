#!/bin/sh
# Installed as /usr/local/bin/factory-sandbox-check. Runs tools/sandbox_e2e.py
# (the real claude CLI against a scripted fake Messages API; no key, no account)
# the way a worker runs: as the unprivileged `worker` identity with NO
# capabilities and no_new_privs, so the result says whether bubblewrap can build
# the sandbox for a worker in THIS container.
#
#   kubectl -n factory exec deploy/factory -- factory-sandbox-check        (AKS)
#   docker run --rm <image> factory-sandbox-check
#
# Why not simply `python sandbox_e2e.py`: in a shell opened by `exec` you are root,
# and the manager runs as `factory` with ambient CAP_SETUID/CAP_SETGID/CAP_KILL. Neither is
# what a worker sees (see image/entrypoint.sh, src/factory/execnocaps.py), and
# bubblewrap behaves differently for both. Needs root, or the factory identity's
# ambient CAP_SETUID/CAP_SETGID, to switch to the worker uid.
#
# The bounding set is left alone (the factory identity cannot change it: no
# CAP_SETPCAP); with empty permitted/effective/ambient sets and no_new_privs it
# cannot be used.
#
# The image sets FACTORY_REQUIRE_WORKER_IDENTITY=1 so that the manager can never
# start a worker as itself. This check is already the worker (setpriv below), and
# sandbox_e2e.py calls run_agent without user=/group=, so with the flag still set
# run_agent would refuse before claude starts and every expectation would fail.
#
# Exit status 0 means every expectation held.
set -eu
HOME=/tmp
FACTORY_REQUIRE_WORKER_IDENTITY=0
export HOME FACTORY_REQUIRE_WORKER_IDENTITY
exec /usr/bin/setpriv \
	--reuid=10002 --regid=10010 --clear-groups \
	--inh-caps=-all --ambient-caps=-all \
	--no-new-privs \
	-- /opt/factory/bin/python /opt/factory-tools/sandbox_e2e.py "$@"
