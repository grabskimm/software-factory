# Factory runtime image: manager + supervisor + Claude Code workers, for the optional
# Azure (AKS) reference deployment in infra/. Build, bump and verification procedures:
# image/README.md.
#
#   docker build -t factory:dev .
#   bash image/smoke.sh factory:dev
#
# Everything the build fetches is pinned and verified:
#   base image         digest (BASE_IMAGE_DIGEST)
#   gh                 release tarball + sha256 (GH_VERSION, GH_SHA256)
#   terraform          release zip + sha256 (TERRAFORM_VERSION, TERRAFORM_SHA256);
#                      only `terraform fmt` is meant to be used, no daemon, no network
#   Node.js 22         release tarball + sha256 (NODE22_VERSION, NODE22_SHA256);
#                      build stage for Claude Code and the optional node toolchain
#   Claude Code        npm lockfile with sha512 integrity (image/claude/)
#   Python deps        pip --require-hashes (image/*.requirements.txt)
# apt packages follow the Debian bookworm archive: apt cannot pin reliably across
# rebuilds, so the immutable artifact is the IMAGE DIGEST that the build reports (push it
# from your own pipeline or `docker buildx build --push`) and Terraform pins ("Pinned git"
# in image/README.md). GIT_VERSION pins git exactly when set.
#
# Linux/amd64 only (gh, Node.js and Claude Code are fetched for x86-64).
# Needs BuildKit (RUN --mount): Docker 23+ or `docker buildx build`.

# MCR mirror of the Docker Hub official image: no Docker Hub anonymous pull limit
# on CI runners. Digest of the multi-arch index (contains linux/amd64), resolved
# from MCR on 2026-11-06:
#   curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' \
#     https://mcr.microsoft.com/v2/mirror/docker/library/debian/manifests/bookworm-slim
ARG BASE_IMAGE=mcr.microsoft.com/mirror/docker/library/debian:bookworm-slim
ARG BASE_IMAGE_DIGEST=sha256:b4aa902587c2e61ce789849cb54c332b0400fe27b1ee33af4669e1f7e7c3e22f

FROM ${BASE_IMAGE}@${BASE_IMAGE_DIGEST} AS base
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN test "$(dpkg --print-architecture)" = amd64 \
    || { echo "this image is linux/amd64 only (build with --platform linux/amd64)" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Download stage: curl + CA certificates, never part of the final image
# ---------------------------------------------------------------------------
FROM base AS fetch
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# GitHub CLI: release tarball, sha256 verified
#   release   https://github.com/cli/cli/releases/tag/v2.103.3
#   checksums https://github.com/cli/cli/releases/download/v2.103.3/gh_2.103.3_checksums.txt
# ---------------------------------------------------------------------------
FROM fetch AS gh
ARG GH_VERSION=2.103.3
ARG GH_SHA256=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
RUN set -eux; \
    curl --fail --silent --show-error --location --retry 3 \
        --output /tmp/gh.tar.gz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz"; \
    echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum --check --strict -; \
    mkdir /tmp/gh; \
    tar -xzf /tmp/gh.tar.gz -C /tmp/gh --strip-components=1 --no-same-owner; \
    install -D -m 0755 /tmp/gh/bin/gh /out/gh; \
    /out/gh --version

# ---------------------------------------------------------------------------
# Verification tools, tier 1 of docs/verification-tiers.md: small, pinned, no
# network, no privilege, no daemon. terraform is a static Go binary from the upstream
# release, sha256 verified; it needs nothing at run time. This pin is deliberately
# independent of any CI pin: scan the image with Trivy before it is pushed (a fixable
# HIGH or CRITICAL finding in the Go standard library of a binary is a reason not to
# push it), so a pin moves when the scan says so, and never by suppression
# (image/README.md, "Bumping a pin").
#
# actionlint is deliberately NOT here. Its newest release (1.7.12) is built with Go
# 1.26.1 and a Trivy scan of an image that carried it listed 24 fixable HIGH findings in
# that standard library, which such a scan gate would refuse. Lint workflows with
# actionlint outside the image instead (image/README.md, "Deliberately not in the
# image").
#
# terraform, for `terraform fmt` (and nothing that needs providers, a backend or
# credentials). BUSL-licensed upstream. CHECKPOINT_DISABLE=1 keeps it from calling
# home; the runtime stage sets it for good.
#   release   https://releases.hashicorp.com/terraform/1.17.1/
#   checksums https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_SHA256SUMS
# ---------------------------------------------------------------------------
FROM fetch AS terraform
ARG TERRAFORM_VERSION=1.17.1
ARG TERRAFORM_SHA256=4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061
RUN set -eux; \
    curl --fail --silent --show-error --location --retry 3 \
        --output /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip"; \
    echo "${TERRAFORM_SHA256}  /tmp/terraform.zip" | sha256sum --check --strict -; \
    mkdir /tmp/terraform; \
    unzip -q /tmp/terraform.zip terraform -d /tmp/terraform; \
    install -D -m 0755 /tmp/terraform/terraform /out/terraform; \
    [ "$(CHECKPOINT_DISABLE=1 /out/terraform version | sed -n 1p)" = "Terraform v${TERRAFORM_VERSION}" ]

# ---------------------------------------------------------------------------
# Node.js 22: build stage for Claude Code and, on request, the node toolchain.
# SUBSTITUTION: a digest-pinned node:22-bookworm-slim from MCR would be the first choice,
# but the MCR mirror (mcr.microsoft.com/mirror/docker/library/node) carries no
# Node 22 tag (checked 2026-11-06; its newest tag is 20-bookworm-slim). The
# official tarball is what the Docker "node" image itself installs, so this stage
# has the same /usr/local layout, pinned by sha256 instead of image digest.
#   https://nodejs.org/dist/v22.23.4/SHASUMS256.txt
# ---------------------------------------------------------------------------
FROM fetch AS node-22
ARG NODE22_VERSION=22.23.4
ARG NODE22_SHA256=b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406
RUN set -eux; \
    curl --fail --silent --show-error --location --retry 3 \
        --output /tmp/node.tar.gz \
        "https://nodejs.org/dist/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-x64.tar.gz"; \
    echo "${NODE22_SHA256}  /tmp/node.tar.gz" | sha256sum --check --strict -; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.gz; \
    [ "$(node --version)" = "v${NODE22_VERSION}" ]; \
    npm --version

# ---------------------------------------------------------------------------
# Claude Code: npm with a committed lockfile (image/claude/), then ONLY the
# native binary is copied out; the final image has no node.
#   * The native binary is taken from the platform package, not from
#     bin/claude.exe. npm treats a failed optional dependency as non-fatal: with a
#     wrong integrity the platform package is dropped, and the postinstall's hard
#     link can outlive it (reproduced). The explicit path plus the checks below
#     fail the build instead. For the same reason scripts are not run
#     (--ignore-scripts): nothing in the final image needs the postinstall.
#   * A glibc image: package @anthropic-ai/claude-code-linux-x64 (libc glibc).
# ---------------------------------------------------------------------------
FROM node-22 AS claude
# Must equal the version pinned in image/claude/package.json and its lockfile.
ARG CLAUDE_CODE_VERSION=2.1.318
ENV HOME=/tmp \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    npm_config_update_notifier=false \
    npm_config_audit=false \
    npm_config_fund=false
WORKDIR /build
COPY image/claude/package.json image/claude/package-lock.json ./
RUN set -eux; \
    pinned="$(node -p "require('./package.json').dependencies['@anthropic-ai/claude-code']")"; \
    if [ "$pinned" != "$CLAUDE_CODE_VERSION" ]; then \
        echo "CLAUDE_CODE_VERSION=$CLAUDE_CODE_VERSION but image/claude/package.json pins $pinned: change package.json, package-lock.json and this ARG together" >&2; \
        exit 1; \
    fi; \
    npm ci --omit=dev --ignore-scripts; \
    bin=node_modules/@anthropic-ai/claude-code-linux-x64/claude; \
    if [ ! -f "$bin" ]; then \
        echo "native Claude Code binary missing: npm skipped the platform package (integrity failure or wrong libc)" >&2; \
        exit 1; \
    fi; \
    [ "$(head -c 4 "$bin" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ] \
        || { echo "$bin is not an ELF executable" >&2; exit 1; }; \
    [ "$("$bin" --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)" ] \
        || { echo "$bin does not report version ${CLAUDE_CODE_VERSION}" >&2; exit 1; }; \
    install -D -m 0755 "$bin" /out/claude; \
    sha256sum /out/claude

# ---------------------------------------------------------------------------
# Per-repo toolchains (build arg TOOLCHAINS, space separated name@major).
# Fail closed: only node@22 has a pinned stage; anything else stops the build
# (image/toolchains.sh, image/README.md "Toolchains").
# ---------------------------------------------------------------------------
FROM base AS toolchains
ARG TOOLCHAINS=""
RUN --mount=type=bind,from=node-22,source=/usr/local,target=/mnt/node-22 \
    --mount=type=bind,source=image/toolchains.sh,target=/tmp/toolchains.sh \
    bash /tmp/toolchains.sh "${TOOLCHAINS}"

# ---------------------------------------------------------------------------
# Python environments, built with the Debian python3 that the final image uses.
#   /opt/factory       the stdlib-only factory package (no runtime dependencies)
#   /opt/token-broker  cryptography and its dependencies, hash-locked, plus
#                      tools/token_broker.py
# The build backend (setuptools) is hash-locked too and builds with
# --no-build-isolation. pip and setuptools are uninstalled afterwards, so the
# final image carries neither (and no stale bundled copies for scanners to flag).
# ---------------------------------------------------------------------------
FROM base AS python-build
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY image/factory-build.requirements.txt image/token-broker.requirements.txt /reqs/
COPY pyproject.toml README.md LICENSE NOTICE /src/
COPY src /src/src
RUN set -eux; \
    pip_flags="--no-cache-dir --disable-pip-version-check"; \
    python3 -m venv /opt/factory; \
    /opt/factory/bin/python -m pip install $pip_flags --only-binary=:all: --require-hashes --no-deps \
        -r /reqs/factory-build.requirements.txt; \
    /opt/factory/bin/python -m pip install $pip_flags --no-build-isolation --no-deps /src; \
    /opt/factory/bin/python -m pip uninstall -y pip setuptools; \
    python3 -m venv /opt/token-broker; \
    /opt/token-broker/bin/python -m pip install $pip_flags --only-binary=:all: --require-hashes --no-deps \
        -r /reqs/token-broker.requirements.txt; \
    /opt/token-broker/bin/python -m pip uninstall -y pip setuptools; \
    /opt/factory/bin/python -c "import factory.cli"; \
    /opt/token-broker/bin/python -c "import cryptography"
COPY tools/token_broker.py /opt/token-broker/token_broker.py

# ---------------------------------------------------------------------------
# Runtime image
# ---------------------------------------------------------------------------
FROM base AS runtime
ARG DEBIAN_FRONTEND=noninteractive
# Optional exact apt version of git, for example 1:2.39.5-0+deb12u2. Unset = the
# archive's current version (recorded in /usr/share/factory-image/versions.txt).
ARG GIT_VERSION=""
# util-linux provides setpriv for the entrypoint. The base snapshot can be months
# old, so security updates are applied first (apt-get upgrade); that is also why
# the image digest, not this file, is what gets promoted. jq, make and shellcheck are
# the apt part of the verification tools (tier 1, docs/verification-tiers.md).
RUN set -eux; \
    apt-get update; \
    apt-get upgrade -y --no-install-recommends; \
    git_pkg=git; \
    if [ -n "${GIT_VERSION}" ]; then git_pkg="git=${GIT_VERSION}"; fi; \
    apt-get install -y --no-install-recommends \
        "${git_pkg}" python3 bubblewrap socat tini ca-certificates util-linux curl \
        jq make shellcheck; \
    rm -rf /var/lib/apt/lists/*

# Users. factory runs the manager and supervisors; worker runs claude. They share
# only the group factory-work: the supervisor's PRIMARY group is its own (factory,
# so files it creates are private to it) and factory-work is a supplementary group
# of the supervisor only, used to share worktrees with the worker (permissions.py).
# Neither user has a password or a login shell that can be used remotely; homes are
# private (the worker's HOME is a per-invocation directory anyway, see agent.py).
RUN set -eux; \
    groupadd --gid 10001 factory; \
    groupadd --gid 10010 factory-work; \
    useradd --no-log-init --uid 10001 --gid 10001 --groups 10010 --create-home --home-dir /home/factory --shell /bin/bash factory; \
    useradd --no-log-init --uid 10002 --gid 10010 --create-home --home-dir /home/worker --shell /usr/sbin/nologin worker; \
    chmod 0700 /home/factory /home/worker; \
    mkdir -p /data /config /opt/toolchains

# Root-owned, world-readable code. The GitHub App key and tokens never touch the
# image or these trees.
COPY --from=gh /out/gh /usr/local/bin/gh
COPY --from=terraform /out/terraform /usr/local/bin/terraform
COPY --from=claude /out/claude /usr/local/bin/claude
COPY --from=python-build /opt/factory /opt/factory
COPY --from=python-build /opt/token-broker /opt/token-broker
COPY --from=toolchains /out/opt/toolchains /opt/toolchains
# Create these directories explicitly: with BuildKit >= 0.23, COPY --chmod=0644
# file /dir/file also applies 0644 to a parent directory that the COPY creates,
# which would make /etc/claude-code unreadable for the worker. Existing directories
# keep their mode, so the result no longer depends on the BuildKit version.
RUN install -d -o root -g root -m 0755 /etc/claude-code /opt/factory-tools
COPY --chmod=0755 image/entrypoint.sh /usr/local/bin/factory-entrypoint
COPY --chmod=0755 image/sandbox-check.sh /usr/local/bin/factory-sandbox-check
COPY --chmod=0644 tools/sandbox_e2e.py /opt/factory-tools/sandbox_e2e.py
# Non-overridable Claude Code baseline; a test keeps it equal to
# factory.phases.managed_settings().
COPY --chmod=0644 managed-settings.json /etc/claude-code/managed-settings.json
RUN ln -s /opt/factory/bin/factory /usr/local/bin/factory

# FACTORY_TOKEN_BROKER: command line the supervisor runs for GitHub App tokens
# (github_auth.from_environment shlex-splits it); the broker venv holds cryptography.
# The three Claude Code switches match what agent.py sets for every worker.
# PATH ends with the optional node toolchain, so it can never shadow a system tool.
# CHECKPOINT_DISABLE: terraform must never call home (workers have no network anyway).
ENV FACTORY_CONFIG=/config \
    FACTORY_DATA=/data \
    FACTORY_REQUIRE_WORKER_IDENTITY=1 \
    FACTORY_MANAGER_HOST=0.0.0.0 \
    FACTORY_MANAGER_PORT=8080 \
    FACTORY_TOKEN_BROKER="/opt/token-broker/bin/python /opt/token-broker/token_broker.py" \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    GH_NO_UPDATE_NOTIFIER=1 \
    GH_PROMPT_DISABLED=1 \
    CHECKPOINT_DISABLE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/toolchains/node/bin

# Record what was installed and fail the build if the pins did not take effect.
RUN set -eux; \
    export HOME="$(mktemp -d)"; \
    mkdir -p /usr/share/factory-image; \
    { \
        echo "git: $(git --version)"; \
        echo "gh: $(gh --version | head -n 1)"; \
        echo "claude: $(claude --version)"; \
        echo "bwrap: $(bwrap --version)"; \
        echo "python3: $(python3 --version)"; \
        echo "tini: $(tini --version)"; \
        echo "setpriv: $(setpriv --version)"; \
        echo "jq: $(jq --version)"; \
        echo "make: $(make --version | sed -n 1p)"; \
        echo "shellcheck: $(shellcheck --version | sed -n 2p)"; \
        echo "terraform: $(terraform version | sed -n 1p)"; \
        echo "bwrap mode: $(stat -c '%a' /usr/bin/bwrap)"; \
        dpkg-query -W -f 'deb: ${Package} ${Version}\n' \
            git bubblewrap socat tini python3 util-linux ca-certificates curl jq make shellcheck; \
    } | tee /usr/share/factory-image/versions.txt; \
    rm -rf "$HOME"; \
    id factory; id worker; \
    test "$(id -u factory)" = 10001; test "$(id -u worker)" = 10002; \
    test "$(id -g factory)" = 10001; test "$(id -G factory)" = "10001 10010"; \
    test "$(id -g worker)" = 10010; test "$(id -G worker)" = 10010; \
    /opt/factory/bin/python -c "import json; json.load(open('/etc/claude-code/managed-settings.json'))"

LABEL org.opencontainers.image.title="factory" \
      org.opencontainers.image.description="Factory manager, supervisor and Claude Code workers" \
      io.factory.git-version-pin="${GIT_VERSION:-distro}"

EXPOSE 8080
# The cluster uses the probes in infra/k8s.tf; this lets `docker run` and other
# runtimes see the same thing. /healthz needs no authentication.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/opt/factory/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('FACTORY_MANAGER_PORT', '8080'), timeout=3).read()"]
# There is deliberately no USER instruction. The container starts as root so that the
# entrypoint can prepare /data (chown on the NFS share) and then give up root, keeping
# only the ambient capabilities setuid, setgid and kill (image/entrypoint.sh), which a
# process that STARTS as a non-root user could not obtain. The manager, every supervisor
# and every git/gh child run as uid 10001 and workers as uid 10002 without capabilities
# (src/factory/execnocaps.py); nothing long-lived runs as root.
WORKDIR /home/factory
ENTRYPOINT ["tini", "--", "/usr/local/bin/factory-entrypoint"]
CMD ["/opt/factory/bin/python", "-m", "factory.manager"]
