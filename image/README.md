# Container image

The Dockerfile at the repository root builds the image that the AKS Deployment
runs: the manager (`python -m factory.manager`), the per-repository supervisors
(`factory ...`) and the Claude Code workers (`claude -p`) as subprocesses. Linux
amd64 only.

**Whether this image is deployable depends on three runtime facts** that only a real cluster
shows: the bubblewrap sandbox starts with `failIfUnavailable`; git worktrees, `os.replace` and
`flock` work on the data store; and `Popen(user=worker)` works with the ambient
`CAP_SETUID`/`CAP_SETGID`/`CAP_KILL`, the worker is left with no capabilities and cannot read the
supervisor's `/proc/<pid>/environ`. A failure of any of them stops the deployment; nothing in this
image weakens the sandbox to get around one. A build and `image/smoke.sh` prove that the image builds
and that its contents and privilege model are right; they cannot prove those three runtime facts.
`factory-sandbox-check` (below) is the check for the first one, run inside the pod.

What was found on the reference deployment (2026-11-06, this image unchanged): the sandbox did not start
on Azure Container Apps and starts on AKS, where the pod needs `hostUsers: false`, `procMount:
Unmasked` and `appArmorProfile: Unconfined`; the data store works on Azure Files NFS (Container Apps)
and on an Azure Disk (AKS), and since a `hostUsers: false` pod cannot mount NFS, `/data` is a disk;
the uid split and the capability model work on both. The image needs nothing different for any of
this.

## What is in the image

| Path | Content | Owner / mode |
|---|---|---|
| `/usr/local/bin/claude` | Claude Code native binary, version pinned in `image/claude/` | root, 0755 |
| `/usr/local/bin/gh` | GitHub CLI, release tarball verified by sha256 | root, 0755 |
| `/usr/bin/git`, `bwrap`, `socat`, `tini`, `setpriv`, `curl`, `python3` | Debian bookworm packages | root |
| `jq`, `make`, `shellcheck` (in `/usr/bin`) | Debian bookworm packages, tier 1 verification tools: JSON checks, a repository's own make targets, shell linting | root |
| `/usr/local/bin/terraform` | Terraform 1.17.1, release zip verified by sha256; tier 1: `terraform fmt -check` only (no providers, no backend, no credentials; `CHECKPOINT_DISABLE=1`) | root, 0755 |
| `/opt/factory` | virtualenv with the stdlib-only `factory` package (`/usr/local/bin/factory` links to it) | root, world-readable code |
| `/opt/token-broker` | virtualenv with `cryptography` (hash-locked) plus `token_broker.py` | root, world-readable code |
| `/opt/factory-tools/sandbox_e2e.py` | the sandbox end-to-end check, run by `factory-sandbox-check` | root |
| `/opt/toolchains/<name>` | optional per-repo toolchains (build arg `TOOLCHAINS`) | root |
| `/etc/claude-code/managed-settings.json` | the repository's `managed-settings.json` (a test keeps it equal to `factory.phases.managed_settings()`) | root, 0644 |
| `/usr/local/bin/factory-entrypoint` | `image/entrypoint.sh` | root, 0755 |
| `/usr/local/bin/factory-sandbox-check` | `image/sandbox-check.sh` | root, 0755 |
| `/usr/share/factory-image/versions.txt` | what was installed (git, gh, claude, bwrap, ...) | root |
| `/data`, `/config` | mount points (`FACTORY_DATA`, `FACTORY_CONFIG`) | prepared by the entrypoint |

Users: groups `factory` (gid 10001) and `factory-work` (gid 10010); `factory` (uid
10001, primary group `factory`, supplementary group `factory-work`, home
`/home/factory`) runs the manager and supervisors; `worker` (uid 10002, primary group
`factory-work`, home `/home/worker`, login shell `/usr/sbin/nologin`, no password) runs
`claude`. The two share only `factory-work`, and only the supervisor-created things
that are deliberately chgrp'd to it (worktrees, the per-invocation home) are visible
to the worker; everything else the supervisor creates belongs to the private group
`factory`.
Both homes are mode 0700. `/opt/factory` and `/opt/token-broker` are code only,
root-owned and world-readable; the GitHub App key and tokens never touch the image
or any file (the key arrives in the environment of the pod, the token
lives in the supervisor's memory).

Image defaults (`ENV`): `FACTORY_CONFIG=/config`, `FACTORY_DATA=/data`,
`FACTORY_REQUIRE_WORKER_IDENTITY=1` (every repository config must set `[worker] uid = 10002` and
`gid = 10010`; without them `run_agent` refuses, because bubblewrap cannot start in a process that
holds the supervisor's ambient capabilities),
`FACTORY_MANAGER_HOST=0.0.0.0`, `FACTORY_MANAGER_PORT=8080`,
`FACTORY_TOKEN_BROKER="/opt/token-broker/bin/python /opt/token-broker/token_broker.py"`,
`DISABLE_AUTOUPDATER=1`, `DISABLE_TELEMETRY=1`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `GH_NO_UPDATE_NOTIFIER=1`,
`GH_PROMPT_DISABLED=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`. The
Terraform side still has to supply `ANTHROPIC_API_KEY`, `GITHUB_APP_PRIVATE_KEY`,
`FACTORY_GITHUB_APP_ID`, `FACTORY_GITHUB_APP_SLUG`, `FACTORY_GITHUB_APP_USER_ID`,
the lock account (`FACTORY_LOCK_ACCOUNT_URL`; the workload-identity variables `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE` and `AZURE_AUTHORITY_HOST` are injected by the AKS
workload-identity webhook) and the manager's authentication
(`FACTORY_MANAGER_AUTH=entra-jwt`, `FACTORY_ENTRA_TENANT_ID`, `FACTORY_ENTRA_AUDIENCES`; `docs/manager.md`).
Because `FACTORY_TOKEN_BROKER` is set, the image is in GitHub App mode:
`github_auth.from_environment` fails loudly if the other three App variables are
missing. For a local run with your own `gh` login set `FACTORY_TOKEN_BROKER=` (empty).

## Verification tools (tier 1)

`jq`, `make`, `shellcheck` and `terraform` are the tools of tier 1 in
`docs/verification-tiers.md`, which also says what tiers 2 to 4 are for. The principle is
**Broad tools, not broad privileges**: a tool is in the image only if it is small, pinned,
needs no network, no privilege, no daemon and no vulnerability database of its own, and is
clean under a blocking Trivy scan. Everything else belongs in your own CI (tier 2) or an isolated
runner (tiers 3 and 4), never next to the supervisor.

* `jq`, `make` and `shellcheck` come from the Debian archive like `git` and `python3`: the
  image digest is the pin and `versions.txt` records the exact `.deb` versions (bookworm:
  `jq` 1.6, `shellcheck` 0.9.0, `make` 4.3, older than a GitHub runner's, so where the two
  disagree your CI is the authority).
* `terraform` is fetched in its own Dockerfile stage from the upstream release, checked
  with `sha256sum --check --strict`, installed with mode 0755 and copied into the final
  image without `--chmod` (see the BuildKit note at the end). The stage fails the build
  if the binary reports another version than the pin. `terraform` is BUSL-licensed
  upstream. Its use is `terraform fmt`; `init`, `validate` and `plan` need
  provider downloads and credentials and do not work in a worker.
* The version is pinned **independently of any CI you run for Terraform** (that CI pins
  its own release): the Go binary is scanned by Trivy, a fixable HIGH or
  CRITICAL finding in its Go standard library or modules is a reason not to push the image to the registry,
  and the image pin must be free to move when the scan says so. `tests/test_image_tooling.py` only requires that two files naming the same
  version name the same hash.
* `image/smoke.sh` runs each tool as the worker (uid 10002, no capabilities) with an input it
  must accept and one it must reject, checks the `terraform` version
  against the Dockerfile pin, and fails if `docker`, `dockerd` or `trivy` is in the image.
* Not proven without a build: that the Docker build, the smoke run and the Trivy scan
  succeed (see "Build and check"). If the scan reports a fixable HIGH or CRITICAL
  finding in a tool added here, bump that tool to a release without it or **leave the tool
  out and say why in this README**. There is no suppression and no waiver for these tools.

## Deliberately not in the image

Tier 1 is as far as the supervisor image goes. These are absent on purpose, and
`tests/test_image_tooling.py` and `image/smoke.sh` keep the first three out, and the tests keep `actionlint` out too:

| Absent | Why |
|---|---|
| `docker` (client) and `dockerd` (daemon), or Docker-in-Docker | needs privileges and a daemon next to the Anthropic key, the Key Vault identity and the GitHub App credentials. Explicitly not planned |
| any privileged container, container runtime socket or extra capability | the pod's one relaxation is the three sandbox settings (`hostUsers: false`, `procMount: Unmasked`, `appArmorProfile: Unconfined`); nothing is added for tooling |
| `trivy` | a scanner with its own vulnerability database and network access; the scan runs outside the image, against the built image |
| `actionlint` | the newest release (1.7.12) is built with Go 1.26.1, and a Trivy scan of the image on this change reported 24 fixable HIGH findings in that Go standard library (`crypto/x509`, `crypto/tls`, `net/http`, `net/mail` and others; the fixed Go versions go up to 1.26.10 and 1.27.3). A blocking scan would refuse the push, no newer release exists to bump to, and a suppression is not allowed. Lint workflows with `actionlint` in your own CI (tier 2). Bring it back only with a release built with a fixed Go and a clean Trivy table |
| browsers (Playwright and the like) | hundreds of MB and many CVEs; a browser test belongs in your own CI (tier 2) |
| `pytest` and other repository toolchains | a repository's own dependencies come from its own CI; only the optional pinned `node@22` exists (`TOOLCHAINS`) |
| a runner base image (GitHub Actions runner image or similar) | tens of GB and hundreds of CVEs: it cannot pass a blocking Trivy scan and would slow every rollout on the single node |
| `npm` and `npx` | their dependency tree failed the Trivy scan (see "Toolchains") |

## Build and check

```sh
docker build -t factory:dev .                 # BuildKit; docker 23+ or docker buildx build
bash image/smoke.sh factory:dev                # one PASS/FAIL line per check, exit 1 on any FAIL
SMOKE_SANDBOX=1 bash image/smoke.sh factory:dev   # also runs factory-sandbox-check (see below)
```

Build arguments (all optional; the defaults are the pins described below):
`BASE_IMAGE`, `BASE_IMAGE_DIGEST`, `GH_VERSION`, `GH_SHA256`,
`TERRAFORM_VERSION`, `TERRAFORM_SHA256`, `NODE22_VERSION`,
`NODE22_SHA256`, `CLAUDE_CODE_VERSION`, `GIT_VERSION`, `TOOLCHAINS`.

To publish, tag and push the image to your registry (`docker tag`, `docker push`; the
registry login server is assumed to be `<ACR_NAME>.azurecr.io`). The reference deployment builds
and scans the image in CI with the same steps (build, `smoke.sh`, Trivy with `severity: HIGH,CRITICAL`
and `ignore-unfixed: true`) and pushes it by digest; that workflow is not part of this repository.
The repository name in the registry defaults to `factory`; the Terraform variable `image_repository`
**must equal it**, otherwise the Deployment asks for a digest that does not exist under that name. If
you push from a pipeline, use a **dedicated** identity (Entra app or user-assigned managed identity)
that holds `AcrPush` on the registry and nothing else, and not the identity that plans or applies
the infrastructure code: that one may run code from pull requests and must not be able to push images.
The pushed digest (`docker push` prints it) is the immutable artifact. **Promotion is setting the
Terraform variable `image_digest` to that digest** (`infra/README.md`, "Promote an image digest").

What only a real build and the cluster can show: that `docker build` succeeds end to end (apt,
Debian package versions, `COPY --chmod`, bind mounts), that the smoke checks of the tier 1 tools
pass as the worker inside the image, that Trivy is clean for the Go binaries, the size of the real
image, and the three runtime facts at the top of this page. The tools reach a pod only after the image digest is
promoted and applied.

## What "pinned" means

| Input | Pinned by | Where |
|---|---|---|
| Base image | digest of the multi-arch index | `BASE_IMAGE_DIGEST` in the Dockerfile |
| gh | release tarball, sha256 from the release's checksums file, checked with `sha256sum -c` | `GH_VERSION`, `GH_SHA256` |
| terraform | release zip, sha256 from `terraform_<v>_SHA256SUMS`, checked with `sha256sum --check --strict`; the stage also checks `terraform version` | `TERRAFORM_VERSION`, `TERRAFORM_SHA256` |
| Node.js 22 (build stage and toolchain) | official tarball, sha256 from `SHASUMS256.txt` | `NODE22_VERSION`, `NODE22_SHA256` |
| Claude Code | exact version in `package.json`, sha512 integrity of the main and platform packages in `package-lock.json` | `image/claude/`, `CLAUDE_CODE_VERSION` |
| factory build backend, token broker dependencies | `pip --require-hashes --only-binary=:all:` | `image/factory-build.requirements.txt`, `image/token-broker.requirements.txt` |
| GitHub Actions | commit SHA | `.github/workflows/ci.yml` |
| Debian packages (`git`, `bubblewrap`, `socat`, `tini`, `python3`, `jq`, `make`, `shellcheck`, ...) | **not pinned**; see below | `apt-get` in the Dockerfile |

### Pinned git (the documented meaning)

apt cannot pin reliably across rebuilds: the bookworm archive replaces package
versions, and an old version disappears from `bookworm-security`. So "pinned git"
means:

* `GIT_VERSION` (build arg) is an optional exact apt
  version, for example `1:2.39.5-0+deb12u2`. When set the build installs exactly that
  and fails if the archive no longer has it. When unset the build installs the
  archive's current git.
* Either way the build records `git --version` and the `.deb` versions of every apt
  package in `/usr/share/factory-image/versions.txt`, prints them in the build output
  and sets the label `io.factory.git-version-pin` to the pin or `distro`.
  (A Dockerfile cannot compute a label from a command's output in the same build,
  hence a file instead of a label for the resolved version.)
* **The immutable artifact is the image digest** that your build prints and that
  Terraform pins. Nothing reproducible is claimed beyond that digest. The same
  applies to `bubblewrap`, `socat`, `tini` and `python3`. `apt-get upgrade` runs
  first because the base snapshot is old (the MCR mirror tag was built on
  2025-11-17).

## Claude Code through npm

The first choice was a pinned native build. The native installer's download host
(`downloads.claude.ai`, where `claude.ai/install.sh` redirects) is not reachable from
the environment this was built in, so the install could not be verified there. npm is
documented as supported and installs "the same native binary as the standalone
installer" (https://code.claude.com/docs/en/setup, "Install with npm": the package
requires Node.js 22 or later and downloads a native binary that does not use node at
runtime). The committed lockfile gives sha512 integrity for the main package and the
platform packages.

How the binary gets into the image (Dockerfile, stage `claude`):

1. `npm ci --omit=dev --ignore-scripts` in a Node.js 22 build stage, from
   `image/claude/package.json` and `package-lock.json`.
2. Only `node_modules/@anthropic-ai/claude-code-linux-x64/claude` is copied out, to
   `/usr/local/bin/claude`. The final image has no node.
3. The stage fails unless that file exists, starts with the ELF magic and
   `--version` prints exactly `<pin> (Claude Code)`; it also fails if the build arg
   `CLAUDE_CODE_VERSION` differs from `package.json`.

Two findings, both reproduced locally with npm 10.9.9 and Claude Code 2.1.318, that
explain steps 1 and 2:

* The package's `postinstall` (`install.cjs`) hard-links the platform binary over the
  placeholder `bin/claude.exe`. But npm treats a failed **optional** dependency as
  non-fatal: with a wrong integrity in the lockfile `npm ci` printed "tarball data ...
  seems to be corrupted", dropped `claude-code-linux-x64`, still exited 0 ("added 2
  packages"), and `bin/claude.exe` was left holding a binary linked by the postinstall
  from the package that had just been rolled back. Copying `bin/claude.exe` would
  therefore ship something that never passed its integrity check, so the explicit
  platform path is copied and its presence is a build condition. Nothing in the final
  image needs the postinstall, so scripts are not run (the premise of the first design was
  copying `bin/claude.exe`, which needs the scripts).
* The lockfile does not record `libc` for the two Linux x64 packages, so on glibc npm
  installs both `claude-code-linux-x64` (glibc) and `claude-code-linux-x64-musl`. The
  glibc one is the one copied.

## Entrypoint and capability model

`ENTRYPOINT ["tini", "--", "/usr/local/bin/factory-entrypoint"]`, `CMD
["/opt/factory/bin/python", "-m", "factory.manager"]`. The container starts as root;
`image/entrypoint.sh`:

1. creates `/config` and `/data/repos` if missing and sets the data root and `repos`
   to `10001:10010`, mode `0710` (the worker group may traverse, nothing more). All of
   it is best effort and loud: every failure prints a fixed `WARNING` line with the
   path, never an environment value, and the script then tests whether uid 10001 can
   write `repos`. On AKS `/data` is an Azure Disk (ext4) mounted into a pod with `hostUsers: false`;
   container root is then an unprivileged node id, and the `chown` to 10001:10010 works on the
   ID-mapped mount. An NFS volume would not mount at all there.
2. exports `HOME=/home/factory`, then `exec setpriv --reuid=10001 --regid=10001
   --groups=10010 --inh-caps/--ambient-caps/--bounding-set = cap_setuid,cap_setgid,cap_kill
   --no-new-privs -- "$@"`.
3. If it was started with a non-root uid (`docker run --user`, a pod security
   context) it just execs the command.

The resulting process holds `CapInh = CapPrm = CapEff = CapBnd = CapAmb =
00000000000000e0`. CAP_KILL is bit 5, CAP_SETGID bit 6 and CAP_SETUID bit 7, so the mask
is **`0xe0`**. (`0xc00` would be `net_bind_service` and
`net_broadcast`; the first version of this image used `0xc0` for setuid and setgid
only.) The smoke test checks `00000000000000e0`.

Why these three: the supervisor starts each worker with `Popen(user=, group=,
extra_groups=[])`, which needs `setuid`, `setgid` and `setgroups`; and it must be able
to kill the worker's process group (lease loss, timeout, drain, purge: invariant 11),
which `kill(2)` allows between two different UIDs only with `CAP_KILL`. Reproduced on a real
kernel: with only setuid and setgid, `killpg` of the worker's group fails with `EPERM`. Why the uid split exists: a worker that runs as a
different uid cannot read the supervisor's `/proc/<pid>/environ` (the App key, the
installation token, the Anthropic key) or its files.

**no_new_privs is set**, on the strength of a local check (util-linux 2.39, bubblewrap
0.9.0, Linux 6.18, as root with numeric ids) that the whole chain still works under it:
entrypoint, then the supervisor stand-in at uid 10001 with ambient caps, then
`Popen(user=10002)` behind the wrapper below, then `bwrap --unshare-user --unshare-pid
--unshare-net`. It stops the supervisor and every worker from gaining privilege
through a setuid or file-capability binary (`su`, `mount`, ...). It does not remove
ambient capabilities, and bubblewrap does not need privilege here (it creates an
unprivileged user namespace and sets no_new_privs itself). The smoke test checks that
`/usr/bin/bwrap` is not setuid, because a setuid bwrap and no_new_privs do not mix.
If the sandbox check fails only with no_new_privs, the flag is in one place in
`entrypoint.sh`.

### Workers shed the capabilities (`src/factory/execnocaps.py`)

Found while testing the chain above: the kernel clears capabilities on a UID change
only to or from 0, and the ambient set survives `execve`. A worker started by the
capability-holding supervisor would therefore keep `CapEff = 0xe0` as uid 10002, and

* **bubblewrap refuses to run**: `bwrap: Unexpected capabilities but not setuid, old
  file caps config?` (it dies when a non-root process has permitted capabilities;
  source: https://github.com/containers/bubblewrap/blob/v0.9.0/bubblewrap.c, `has_caps`).
  With `sandbox.failIfUnavailable` every worker run would then stop.
* a worker holding `CAP_SETUID` could `setuid()` back to uid 10001 or to root and read
  the supervisor's environment, which defeats the uid split (reproduced).

`run_agent` therefore starts every separate-identity worker as `python -I -S
execnocaps.py -- claude ...`. The wrapper already runs as the worker, clears the ambient
set with `prctl(PR_CAP_AMBIENT_CLEAR_ALL)`, verifies it is empty and execs the real
command, which then has an empty permitted and effective set (no file capabilities in the
image, `no_new_privs` set). It fails closed (exit 126, nothing runs) and prints one fixed
line. The inheritable and bounding sets keep `0xe0` in the worker; with no ambient set,
no file capabilities and `no_new_privs` they cannot be used. (An earlier draft of this
image used a shell launcher through `FACTORY_AGENT_CMD`; the in-package wrapper replaced
it because it also covers `preflight` and cannot be bypassed by overriding that
variable.) Checked locally through the full chain: the worker is uid 10002, gid 10010,
no supplementary groups, `CapPrm/CapEff/CapAmb = 0`, and `bwrap` runs.

`tools/sandbox_e2e.py` runs `claude` as the current user. Run it inside the container
with `factory-sandbox-check`, which starts it as the worker identity with no
capabilities and no_new_privs (a shell opened by `kubectl exec` is root, which
is neither what a worker sees nor what bubblewrap needs to work as):

```sh
kubectl -n factory exec deploy/factory -- factory-sandbox-check    # exit 0 = every expectation held
```

That command passed in the AKS pod with the three settings together (`hostUsers: false`,
`procMount: Unmasked`, `appArmorProfile: Unconfined`; the smaller sets tried failed, two subsets were not tried). A pod without them can show
`bwrap: Failed to make / slave: Permission denied` or `Can't mount proc on /newroot/proc`.

## Toolchains

Build arg `TOOLCHAINS` is a space separated list of `name@major` from the repos'
`toolchains` config, for example `TOOLCHAINS="node@22"` (the build argument of your
own build).

* `node@22` copies `/usr/local` of the Node.js 22 stage (pinned by sha256, see above)
  to `/opt/toolchains/node` and then **removes the bundled npm and npx**
  (`lib/node_modules/npm`, `bin/npm`, `bin/npx`); node and corepack stay, so a repo gets
  pnpm (or yarn) through `corepack`. Reason: npm's own dependency tree
  (`brace-expansion`, `ip-address`, `pacote`, `picomatch`, `sigstore`) carried HIGH
  findings that failed the blocking Trivy scan, and no suppression is used. Nothing new
  is downloaded, so no extra hash is needed. `/opt/toolchains/node/bin` is appended to
  `PATH` by the image `ENV`, after the system directories, so it can never shadow `git`,
  `gh` or `python3`. `PATH` is image-wide: the manager, supervisors and workers all see it
  (agent.py forwards `PATH` to workers). The Claude Code build stage and the toolchain
  use the same Node.js stage, and that stage keeps npm (`npm ci` needs it); the final
  image contains node only when asked, and never npm. `smoke.sh` checks, as the worker
  user, `node --version` and `corepack --version`, and that npm/npx are absent.
* **corepack needs the network.** `corepack pnpm ...` downloads pnpm from
  `registry.npmjs.org` on first use, so that host must be on the verify allow-list for
  repositories that need pnpm. That this works under the sandbox is **unverified** until
  a verify preflight runs on a repository that needs pnpm. `npm`/`npx` are not
  available; there is no pinned replacement for them.
* Anything else (`python@3.12`, `pnpm@9`, `node@20`, `node@22.1.0`) **fails the build**
  with a message telling the operator to add a pinned stage to the Dockerfile and a
  `case` to `image/toolchains.sh`. It never installs an unpinned tool from the network.

**GAP.** Only `node@22` is implemented. The operator's `REPO_LIST` (the `toolchains`
of each repo config, which is not in this repository) decides what else is needed; a
repo that needs another toolchain cannot be served by this image until a pinned stage
is written for it. With one image per toolchain set the operator
builds a union of what the configured repos need. Trivy scans the rest of the Node.js
tree (corepack) like the rest of the image; a finding there is fixed by a newer pinned
Node.js release, not by a suppression.

## Substitutions and decisions (with sources)

| Item | Decision | Source |
|---|---|---|
| Base image | `mcr.microsoft.com/mirror/docker/library/debian:bookworm-slim@sha256:b4aa9025...c3e22f` (index, contains linux/amd64 `sha256:72ceb30c...`; the tag resolved to the same digest on 2026-11-06; image created 2025-11-17) | `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json' https://mcr.microsoft.com/v2/mirror/docker/library/debian/manifests/bookworm-slim` |
| Node.js 22 | **Substitution.** A digest-pinned `node:22-bookworm-slim` from MCR would have been the first choice. The MCR mirror has no Node 22 tag (newest `20-bookworm-slim`; every `22*` tag returns 404). The same Docker official image digest (`sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d` for `22-bookworm-slim` on 2026-11-06) is served by Docker Hub and mirror.gcr.io, and public.ecr.aws answered "TOOMANYREQUESTS" from a shared IP, so a registry pull would bring back the anonymous-limit problem MCR was chosen to avoid. The stage installs the official tarball (the same thing that image installs), pinned by sha256: Node.js 22.23.4, 2026-10-20 | https://mcr.microsoft.com/v2/mirror/docker/library/node/tags/list ; https://nodejs.org/dist/v22.23.4/SHASUMS256.txt |
| gh | 2.103.3 (2026-10-26), highest `v2.*` tag, `gh_2.103.3_linux_amd64.tar.gz` sha256 `4c806978...dd91d` | https://github.com/cli/cli/releases/tag/v2.103.3 ; https://github.com/cli/cli/releases/download/v2.103.3/gh_2.103.3_checksums.txt |
| actionlint | **Left out of the image.** 1.7.12 is the newest release on 2026-11-07 (archive timestamp 2026-03-30) and was built with Go 1.26.1. The sha256 `8aca8db9...3d8` had been computed from the downloaded `actionlint_1.7.12_linux_amd64.tar.gz` and equals the line in the release's checksums file, and the stage built, but a Trivy scan of the image on this change reported 24 fixable HIGH findings in its Go standard library, so the tool was removed rather than suppressed (see "Deliberately not in the image"). An `actionlint` job in your CI keeps its own pin | https://github.com/rhysd/actionlint/releases/tag/v1.7.12 ; https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_checksums.txt |
| terraform | 1.17.1 (archive timestamp 2026-10-28), `terraform_1.17.1_linux_amd64.zip` sha256 `46386280...061`, computed from the downloaded file and equal to the upstream `SHA256SUMS` line. Built with Go 1.26.10; the binary is about 129 MB. A newer Go standard library is less likely to carry fixable findings, which this pin is chosen for (not confirmed by a scan). Only `terraform fmt` is used, so the version difference does not matter for what a worker does with it | https://releases.hashicorp.com/terraform/1.17.1/ ; https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_SHA256SUMS |
| Claude Code | 2.1.318 through npm, lockfile integrity | https://code.claude.com/docs/en/setup ; https://registry.npmjs.org/@anthropic-ai/claude-code/2.1.318 |
| `--ignore-scripts` | used; reason above | reproduced locally |
| capability mask and set | `0xc00` is wrong; setuid+setgid is `0xc0`, and with `CAP_KILL` (needed for `kill(2)` across UIDs) it is `0xe0`. The worker sheds them (`execnocaps`). | `capabilities(7)`: CAP_KILL 5, CAP_SETGID 6, CAP_SETUID 7 |
| setpriv options | `--reuid --regid --groups --clear-groups --inh-caps --ambient-caps --bounding-set --no-new-privs` all exist in util-linux 2.38.1 (bookworm) | https://raw.githubusercontent.com/util-linux/util-linux/v2.38.1/sys-utils/setpriv.1.adoc |
| gh environment | `GH_NO_UPDATE_NOTIFIER`, `GH_PROMPT_DISABLED` | https://raw.githubusercontent.com/cli/cli/v2.103.3/pkg/cmd/root/help_topic.go |
| Python | Debian `python3` is 3.11, so `pip-compile` runs on 3.11; wheels `cp311-abi3-manylinux_2_34_x86_64` and friends | `cryptography` 50.0.2, `cffi` 2.1.1, `pycparser` 3.12 on PyPI |
| Build backend | `setuptools` hash-locked, `--no-build-isolation`, then `pip` and `setuptools` removed from both venvs (the venvs carry no stale bundled copies for scanners to flag) | `image/factory-build.requirements.in` |

## Bumping a pin

Always rebuild, run `bash image/smoke.sh`, and re-run the sandbox check (`factory-sandbox-check`) on
AKS after changing the base image or Claude Code (and after a node image or Kubernetes upgrade).

* **Base digest.** `curl -sI -H 'Accept: application/vnd.oci.image.index.v1+json'
  https://mcr.microsoft.com/v2/mirror/docker/library/debian/manifests/bookworm-slim |
  grep -i docker-content-digest`. Confirm the index lists `linux/amd64`:
  `curl -s -H 'Accept: application/vnd.oci.image.index.v1+json' .../manifests/<digest>`.
  Update `BASE_IMAGE_DIGEST`. (Debian 12 is now the oldstable release; check the base
  release before the next bump, `trixie` also exists on the MCR mirror.)
* **gh.** Pick the release, download `gh_<v>_checksums.txt` from it, copy the
  `gh_<v>_linux_amd64.tar.gz` line's sha256. Update `GH_VERSION` and `GH_SHA256`.
* **actionlint (not in the image).** Bring it back only when a release built with a Go
  version past the findings of the Trivy scan exists. Then add a stage like the
  `terraform` one (download, `sha256sum --check --strict` against a hash you computed from
  the real download and compared with the release's checksums file, a version check), the
  `COPY`, the `versions.txt` line and the smoke checks as the worker, restore its tests in
  `tests/test_image_tooling.py`, and read the Trivy table of your image build before merging.
* **terraform.** The same with `terraform_<v>_linux_amd64.zip` and `terraform_<v>_SHA256SUMS`
  from `https://releases.hashicorp.com/terraform/<v>/`. Update `TERRAFORM_VERSION` and
  `TERRAFORM_SHA256`. Any Terraform pin of your own CI is a separate decision.
* **A tier 1 tool fails the Trivy scan.** Read the Trivy table of your image build. For a fixable HIGH or CRITICAL
  finding in `terraform`, `jq` or `shellcheck`: bump the tool to a release that
  fixes it, or, if no such release exists, remove the tool (its Dockerfile stage, `COPY`,
  `versions.txt` line, smoke checks, README row, and its entry in
  `tests/test_image_tooling.py`) and record the reason here. No suppression and no scanner waiver
  for these tools.
* **Node.js 22.** `https://nodejs.org/dist/v<v>/SHASUMS256.txt`, the
  `node-v<v>-linux-x64.tar.gz` line. Update `NODE22_VERSION` and `NODE22_SHA256`.
* **Claude Code.** Edit the version in `image/claude/package.json`, then in
  `image/claude/` run `npm install --package-lock-only --ignore-scripts
  --registry=https://registry.npmjs.org/`, and set `ARG CLAUDE_CODE_VERSION`. Check in
  a scratch directory: `npm ci --omit=dev --ignore-scripts`, `file` on
  `node_modules/@anthropic-ai/claude-code-linux-x64/claude` (ELF), and run it with
  `--version`. The lockfile must contain the `claude-code-linux-x64` entry with an
  `integrity` field. Re-check the CLI surface (flags, tools under `--bare`, managed settings
  keys) against the new version with `claude --help` and `image/smoke.sh`.
* **Hash locks.** With Python 3.11 (the version bookworm ships) from the repository root:
  `pip-compile --generate-hashes --strip-extras --output-file
  image/token-broker.requirements.txt tools/requirements-token-broker.txt` and
  `pip-compile --generate-hashes --strip-extras --allow-unsafe --output-file
  image/factory-build.requirements.txt image/factory-build.requirements.in`
  (`pip install pip-tools` in a throwaway venv). The lists hold the hashes of every file
  PyPI publishes for the pinned versions; the install uses `--only-binary=:all:`, so
  only wheels are ever used.
* **Actions.** `git ls-remote --tags https://github.com/<owner>/<repo>.git`; for an
  annotated tag use the commit on the `^{}` line. Keep the version in the trailing
  comment.
* **Toolchains.** Add a stage and a `case` (see Toolchains).

## Known issue: BuildKit 0.23 and later set 0644 on the parent directory of `COPY --chmod`

Found on 2026-11-06 and **not fixed here**: `COPY --chmod=0644 f /etc/claude-code/f` creates the parent
directory `/etc/claude-code` with mode **0644** on BuildKit 0.23.2 and 0.31.0 (both tested), while
BuildKit 0.20.2 (Docker 28.0.4) gives 0755. The same applies to `/opt/factory-tools`
(the `COPY --chmod` lines of the Dockerfile). A directory without the execute bit cannot be traversed by non-owners, so
`managed-settings.json` becomes unreadable for the supervisor and the workers; `image/smoke.sh` catches it
(2 FAIL on "managed-settings.json readable"). A build passes only while the builder has BuildKit 0.20.x and will
fail when the builder moves on. Until the Dockerfile is changed, build the image with a
`docker-container` builder pinned to `moby/buildkit:v0.20.2` (the reference image was built that way; `az acr
build` is not an option, its classic builder rejects the Dockerfile's `--mount`), and run `bash image/smoke.sh`
on whatever you build. Changing the Dockerfile so that the build no longer depends on that BuildKit version is an open decision.
