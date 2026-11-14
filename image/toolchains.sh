#!/bin/bash
# Fail-closed installer for the per-repo toolchains (Dockerfile build arg
# TOOLCHAINS, space separated name@major, for example "node@22"). It runs in the
# `toolchains` build stage only; nothing here ships as a script in the image.
#
#   toolchains.sh "node@22"
#
# Inputs, bind-mounted read-only by the Dockerfile from digest- or checksum-pinned
# stages:   /mnt/node-22   the /usr/local tree of the pinned Node.js 22 stage
# Output:   /out/opt/toolchains/<name>   copied into the image as /opt/toolchains
#
# Only toolchains with a pinned stage are supported. Anything else FAILS the
# build instead of being installed unpinned from the network: the operator must
# add a pinned stage to the Dockerfile and a case below (image/README.md,
# "Toolchains", documents this as a known gap).
set -euo pipefail

mount_root=${TOOLCHAINS_MOUNT_ROOT:-/mnt}
out=${TOOLCHAINS_OUT:-/out}/opt/toolchains

fail() {
	printf 'toolchains: ERROR: %s\n' "$*" >&2
	exit 1
}

install_node() {
	local major=$1 src=$mount_root/node-$1 dest="$out/node" version
	[ -d "$src" ] || fail "the pinned node $major stage is not mounted at $src"
	if [ -e "$dest" ]; then
		return 0 # listed twice
	fi
	cp -a -- "$src" "$dest"
	# Ship node and corepack without the bundled npm/npx: npm's own dependency tree
	# carries HIGH findings that fail a Trivy scan gate, and repos that need a
	# package manager get pnpm through corepack. (The node-22 stage keeps npm: the
	# Claude Code build stage uses it and it never reaches the final image.)
	rm -rf -- "$dest/lib/node_modules/npm" "$dest/bin/npm" "$dest/bin/npx"
	[ -e "$dest/lib/node_modules/corepack/package.json" ] || fail "the node $major stage has no corepack"
	[ -x "$dest/bin/corepack" ] || fail "the node $major stage has no bin/corepack"
	version=$("$dest/bin/node" --version)
	case "$version" in
	"v$major".*) ;;
	*) fail "expected a node $major toolchain but $dest/bin/node reports $version" ;;
	esac
	PATH="$dest/bin:$PATH" "$dest/bin/corepack" --version >/dev/null || fail "corepack does not run in $dest"
	printf 'toolchains: node %s installed at /opt/toolchains/node (corepack, no npm/npx)\n' "$version"
}

mkdir -p -- "$out"

read -r -a specs <<<"${1:-}"
for spec in "${specs[@]}"; do
	[[ $spec =~ ^[a-z][a-z0-9-]*@[A-Za-z0-9._+-]+$ ]] ||
		fail "'$spec' is not name@version (for example node@22)"
	case "$spec" in
	node@22) install_node 22 ;;
	*)
		fail "unsupported toolchain '$spec'. This image can only install: node@22 (the major selects the pinned stage; exact versions are not accepted). To add anything else, add a pinned stage to the Dockerfile and a case to image/toolchains.sh (see image/README.md, Toolchains), then build again."
		;;
	esac
done
