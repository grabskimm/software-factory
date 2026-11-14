# Software Factory

Software Factory turns a GitHub backlog into pull requests. It groups open issues into coherent
jobs and runs a bounded pool of Claude Code workers (`claude -p` subprocesses) in git worktrees:
plan, build, an independent read-only review, an optional verification, publish. It **stops at a
published pull request plus a `factory/gate` commit status**. It never merges and never closes
issues: GitHub auto-merge and rulesets merge, and `Closes #N` in the pull request body closes.
Every run is a directory of plain files, so it can be resumed and audited.

Python 3.11+, standard library only, plus `git`, `gh` and the Claude Code CLI. The distribution is
called `software-factory` (install from a checkout; it is not published to PyPI), while the import
package and the commands are called `factory` and `factory-manager`. Two optional parts sit on top:
a manager (`factory-manager`: an HTTP API, label-triggered builds, a dashboard and Prometheus metrics)
and a reference deployment of the manager on Azure Kubernetes Service (AKS).

```text
a person with write access adds the label `factory-build` to an open issue
  -> plan ok, label consumed
  -> factory:planned -> factory:building -> factory:in-review -> factory:pr-open -> factory:merged
     (one `factory:*` label at a time; factory:blocked or factory:ci-failed means a person must act)
  -> per job: build -> independent review -> optional verify -> pull request + `factory/gate` status
  -> GitHub auto-merge merges once the gate and CI pass; `Closes #N` closes the issue
```

## Run it locally

Prerequisites:

* Python 3.11 or newer and `git`.
* The GitHub CLI `gh`, logged in (`gh auth login`) as an account that can push branches and open pull
  requests on the target repository.
* The Claude Code CLI, `claude`, on `PATH`.
* `ANTHROPIC_API_KEY` in the environment: the only secret a worker receives. Workers run
  `claude --bare`, so a `claude login` session is not used.
* On Linux, `bubblewrap` and `socat` (`apt install bubblewrap socat`): the worker sandbox is mandatory,
  and a worker fails rather than run unsandboxed. Linux is what is tested; macOS is untested and
  Windows is not supported.

Install and check:

```sh
git clone https://github.com/grabskimm/software-factory
cd software-factory
python3 -m venv .venv && . .venv/bin/activate
pip install -e .      # console scripts `factory` and `factory-manager`; `python -m factory` works too
factory --help        # a safe smoke test: it calls neither GitHub nor Anthropic
```

**Local mode.** From a checkout of a repository whose issues you want worked (it needs an `origin`
remote and a `main` branch), with every config key at its default:

```sh
cd path/to/a-scratch-checkout
export ANTHROPIC_API_KEY=...
factory plan --limit 5             # a planner session groups up to 5 open issues into jobs (or: plan --issues 12,15)
less factory/runs/pilot/plan.json  # read the plan before anything is built
factory run --concurrency 1        # build, review, verify, publish
factory status                     # jobs, pull requests, cost (--offline skips the GitHub lookups)
```

`plan` and `run` call GitHub and the Anthropic API. **`run` pushes real branches
(`factory/<run>/<job>`), opens real pull requests and spends real money**; try it on a scratch
repository or a fork first. Every worker invocation has a dollar cap (by default plan $5, build $8,
review $4, verify $6, preflight $0.50; `--budget-<phase>` changes one for a command). The run state
lives in `factory/runs/pilot` inside the checkout: add `factory/runs/` to that repository's ignore
file, or give another directory with `factory --run-dir /somewhere/r1 plan ...` (a global option, so
it goes before the subcommand). `run` exits 2 while any job is not `pr_ready`, `merged` or `closed`.

**Managed mode** is what a manager uses: `FACTORY_CONFIG` holds one `repos/<org>__<name>.toml` per
repository (the smallest file is `repo = "org/name"`) and `FACTORY_DATA` holds the managed clones and
runs, and commands take `--repo org/name --run-dir "$FACTORY_DATA/repos/org/name/runs/r1"`. Every
config key is in [docs/configuration.md](docs/configuration.md).

**Run the tests.** They pass with only `python3` and `git` (`gh` and `claude` are faked); tests that need optional tools
(for example `cryptography` for the token broker) skip without them:

```sh
pip install -e .
python3 -m unittest discover -s tests       # several thousand tests, on the order of ten minutes
python3 -m unittest discover -s infra/test  # the offline checks of the Terraform reference, seconds
python3 -m unittest tests.test_config       # one module; PYTHONPATH=src works instead of installing
```

Use a normal user: root ignores directory permissions, so as root the permission-based purge tests
(`tests/test_purge.py`, `WorkerOwnedFilesTests`) are skipped. CI (`.github/workflows/ci.yml`) compiles everything, runs the unit
tests on Python 3.11 to 3.13, the token broker tests and the infra offline checks, deploys nothing, and ends
in the one check to require, `CI result` ([docs/handoff.md](docs/handoff.md)).

**The manager locally.** It is optional and needs the same tools as the CLI it drives:

```sh
cd "$(mktemp -d)"     # a scratch directory outside the checkout: the manager's files go to ./.factory
mkdir -p .factory/config/repos .factory/data
export FACTORY_CONFIG=$PWD/.factory/config FACTORY_DATA=$PWD/.factory/data
printf 'repo = "org/name"\n' > .factory/config/repos/org__name.toml    # optional: register a repository
FACTORY_MANAGER_OPERATIONS=off factory-manager                        # 127.0.0.1:8080
curl http://127.0.0.1:8080/healthz                                    # from a second terminal: {"status":"ok"}
```

`FACTORY_MANAGER_OPERATIONS=off` alone makes it passive: it starts no background work (no restart recovery,
cost guardrail, purge or label watcher). Without it the manager polls GitHub every 60 seconds for the
`factory-build` label on each registered repository (the normal mode) and recovers and purges runs;
`FACTORY_MANAGER_TRIGGER_SECONDS=0` turns off only the label watcher.

**There is no unauthenticated mode.** Every call except `/healthz` and the static page files needs an
identity. In the default `easy-auth` mode it is the `X-MS-CLIENT-PRINCIPAL` header (base64 of a JSON
document) that a platform in front normally sets; anything that can reach the port can forge it, so
keep the manager on loopback:

```sh
PRINCIPAL=$(printf '%s' '{"auth_typ":"aad","claims":[{"typ":"oid","val":"local-dev"},{"typ":"roles","val":"Factory.Operator"}],"role_typ":"roles"}' | base64 | tr -d '\n')
curl -H "X-MS-CLIENT-PRINCIPAL: $PRINCIPAL" http://127.0.0.1:8080/repos
```

`FACTORY_MANAGER_AUTH=entra-jwt` instead checks a real Entra bearer token. The browser page at `/ui/`
loads without one but sends only a pasted bearer token, which `easy-auth` mode ignores, so on its own
its data calls get `401`: it needs an authenticating front or `entra-jwt`
([docs/manager.md](docs/manager.md)); do not expect it to work out of the box. Prometheus metrics are
served on a separate listener that is off unless `FACTORY_MANAGER_METRICS_PORT` is set:
`FACTORY_MANAGER_METRICS_PORT=9464 factory-manager`, then `curl http://127.0.0.1:9464/metrics`
(loopback needs no token; any other host needs `FACTORY_MANAGER_METRICS_TOKEN`).

## Dashboards (read-only Grafana)

The three Grafana dashboards of the maintainer's own deployment are public and read-only at
**<https://factory.mendelg.tech/grafana>** (the link opens `Factory overview`; the other two are in the
dashboard list). A visitor cannot change a dashboard, the data source or any setting:

* `Factory overview` (`factory-overview`): jobs by status, jobs and runs finished per day, spend, time from
  plan to pull request, time per phase and worker use. It needs only the manager's own metrics.
* `Factory resources and manager` (`factory-resources`): the manager pod's memory and CPU against its limits, memory
  per session, disk and history size, manager API requests, latency and 401/403 counts.
* `Factory node auto-provisioning` (`factory-nap`): worker pods and the nodes of node auto-provisioning (pending,
  unschedulable, evicted) and the time from pod creation to a ready node.

The boards are defined in `infra/monitoring/dashboards/`, and [docs/metrics.md](docs/metrics.md) describes every
series. Publishing them read-only is the Terraform flag `grafana_anonymous_viewer_enabled` (off by default). Read
"Publishing the dashboards read-only" in [docs/metrics.md](docs/metrics.md) before setting it: anonymous read access
reaches the whole Prometheus data source, not only the panels (repository names, spend, pod and node names, alert rules).

**A local run does not set up metrics or Grafana.** `factory-manager` only serves the Prometheus text endpoint, and
only when `FACTORY_MANAGER_METRICS_PORT` is set (above); nothing starts a Prometheus or a Grafana. To look at the
boards for a local manager:

1. Run a Prometheus that scrapes `http://127.0.0.1:9464/metrics` (the default path) as the job `factory-manager`
   (the `Manager scraped` panel reads `up{job="factory-manager"}`).
2. Give a Grafana a Prometheus data source with the fixed UID `factory-prometheus` (the dashboard JSON hard-codes it
   and has no import prompt), for example with a provisioning file that sets `uid: factory-prometheus`.
3. Import `infra/monitoring/dashboards/factory-overview.json`. `factory-resources` also works for its manager panels
   (API requests, history, disk), but its pod memory and CPU rows read the kubelet's container series, and
   `factory-nap` reads Kubernetes series that exist only on the AKS reference deployment, where `infra/` deploys
   Prometheus and Grafana with `monitoring_enabled = true`.

## Repository layout

```text
src/factory/               the package: CLI, supervisor, planner, runner, manager, GitHub and sandbox code
tests/                     the unit tests
tools/                     token broker, sandbox end-to-end check, reports over run directories, and optional
                           operator scripts: infra_deploy_gate.py, infra_cluster_guard.py, policy_matrix.py,
                           effort_ab.py (an effort A/B harness)
scripts/                   helpers that register a repository with a deployment
handoff/                   the GitHub ruleset and auto-merge workflow an operator applies (nothing installs them)
docs/                      the reference documentation (index below)
image/ Dockerfile          the pinned container image of the optional deployment
managed-settings.json      the Claude Code sandbox baseline, copied into the image
infra/                     Terraform of the optional Azure/AKS reference deployment, with offline tests in infra/test
.github/workflows/ci.yml   the CI: compile, unit tests, token broker, infra offline checks (it deploys nothing)
.claude/skills/factory/    a Claude Code skill for agents that operate Factory
```

## Documentation

* [docs/configuration.md](docs/configuration.md): the per-repository config file, model and effort, labels and comments.
* [docs/architecture.md](docs/architecture.md): the architecture and operation reference (run, hand-off, operating a run, worker boundaries, module layout).
* [docs/manager.md](docs/manager.md): the manager's endpoints, authentication modes and settings.
* [docs/operations.md](docs/operations.md): day-to-day operation of a deployment.
* [docs/handoff.md](docs/handoff.md): the GitHub ruleset and auto-merge that finish what Factory publishes.
* [docs/enrolling-repositories.md](docs/enrolling-repositories.md): registering a repository.
* [docs/metrics.md](docs/metrics.md): the Prometheus metrics and the dashboards.
* [docs/verification-tiers.md](docs/verification-tiers.md): what is verified in the pod, in CI and in isolated runners.
* [docs/capacity.md](docs/capacity.md): the worker budget, the memory guard and sizing.
* [docs/pod-backend.md](docs/pod-backend.md) (experimental, off by default): the optional pod per worker session.
* [infra/README.md](infra/README.md) and [image/README.md](image/README.md): the optional AKS reference deployment and its image.

## Claude Code skill

`.claude/skills/factory/SKILL.md` is a Claude Code skill for agents that operate Factory (plan, run,
monitor, resume and drain through the CLI). It loads in a checkout of this repository; copy its
directory to `~/.claude/skills/` to use it elsewhere. Its references to `docs/` and `handoff/` need a checkout of
this repository.

## Notes

* This is an independent project. It is not affiliated with or endorsed by Anthropic, GitHub or
  Microsoft; Claude and Claude Code are products of Anthropic.
* Issue and pull request numbers in the docs and in code comments refer to the project's original tracker.
* The container image installs Claude Code and Terraform, which have their own license terms; this
  repository holds the recipe and does not redistribute them.
* License: Apache-2.0, see [LICENSE](LICENSE). See also [CONTRIBUTING.md](CONTRIBUTING.md) and
  [SECURITY.md](SECURITY.md).
