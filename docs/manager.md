# Factory manager (remote control surface)

`factory-manager` (`python -m factory.manager`) is a small HTTP server that sits in
front of the `factory` CLI. Every endpoint turns a validated request into the argv of
one CLI command and runs it as a subprocess; the manager reimplements none of the
CLI (the one exception is the read-only dashboard endpoints, which read run files and the history ledger in the
manager's own process; see "Dashboard endpoints" and Deviations; and the static page of the browser UI is the one thing served without a token, see "The browser page"). It authenticates callers in one of two modes (`FACTORY_MANAGER_AUTH`): by default it
trusts the Entra "Easy Auth" identity header that Azure Container Apps adds in front of it
(`easy-auth`); on AKS, where there is no Easy Auth, it verifies an Entra bearer token itself
(`entra-jwt`). See Trust model.

Python standard library only (`http.server.ThreadingHTTPServer`). Source:
`src/factory/manager.py`, `src/factory/entra_jwt.py` (bearer-token verification),
`src/factory/operations.py` (the operations loop), `src/factory/history.py` (the history ledger), `src/factory/dashboard.py`
(the read-only dashboard endpoints) and `src/factory/webui.py` with `src/factory/ui/` (the static browser page). Tests:
`tests/test_manager.py`, `tests/test_history.py` (the ledger), `tests/test_dashboard_api.py` (the dashboard endpoints and the operator entries), `tests/test_webui.py` (the page: routes, headers, allow-list, packaging and, with node, the script), `tests/test_entra_jwt.py` (the token verifier, with RSA keys generated in-process), `tests/test_operations.py` (the loop against scripted fakes) and
`tests/test_operations_integration.py` (the loop, the manager and the CLI together: kill a supervisor
mid-build, recover after a restart, retry, run, purge; see its docstring for what is real and what is faked).

## Configuration

All configuration is environment variables. Values are validated at start; a bad value
prints a message that names the variable (never its value) and exits 2.

| Variable | Default | Meaning |
|---|---|---|
| `FACTORY_CONFIG` | required | Directory holding `repos/<org>__<name>.toml`. A repository is allowlisted if and only if its file exists. |
| `FACTORY_DATA` | required | Data root; run directories are `${FACTORY_DATA}/repos/<org>/<name>/runs/<run>` (from `factory.config.data_paths`). |
| `FACTORY_LOGS_ARCHIVE` | `on` | `off` stops `purge` from copying a run's logs before it deletes them ("Log retention"). Shared with the CLI. |
| `FACTORY_LOGS_ARCHIVE_ACCOUNT_URL` | unset | An Azure storage account's blob endpoint (`https://<account>.blob.core.windows.net`): the copies go to a container of it, with the pod's workload identity. Unset: a directory of the data volume, `${FACTORY_DATA}/logs-archive/`. |
| `FACTORY_LOGS_ARCHIVE_CONTAINER` | `logs-archive` | The container, with an account URL. |
| `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS` | `30` | Days a copy is kept (1 to 3650); the janitor removes older ones once a day. |
| `FACTORY_MANAGER_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only inside the container. |
| `FACTORY_MANAGER_PORT` | `8080` | Bind port; `0` picks a free port (the startup line prints it). |
| `FACTORY_MANAGER_COMMAND_TIMEOUT` | `300` | Seconds a short CLI command may run; on expiry the command's process group is killed and the response is 504. |
| `FACTORY_MANAGER_SHUTDOWN_SECONDS` | `25` | Grace period between SIGINT and SIGKILL for started children at shutdown. |
| `FACTORY_MANAGER_CLI` | `<python> -m factory` | Space-separated argv prefix of the factory CLI. Tests point it at a fake. |
| `FACTORY_MANAGER_PAUSE_SECONDS` | `60` | How long `pause` waits for the supervisor to exit (not in the original spec; see Deviations). |
| `FACTORY_MANAGER_REQUEST_TIMEOUT` | `15` | Socket timeout per `recv`, so a silent client cannot pin a thread (not in the original spec). Total deadlines on top of it: 20 s for the request line and headers, 30 s for the body. |
| `FACTORY_MANAGER_MAX_STATUS_COMMANDS` | `6` | Concurrent `status` commands. More get `503` with `Retry-After: 5` after waiting 2 s for a slot. |
| `FACTORY_MANAGER_MAX_COMMANDS` | `8` | Concurrent other short commands, in a lane of their own, so read-only polling can never starve an operator action. |
| `FACTORY_MANAGER_OPERATIONS` | `on` | `off` disables the whole operations loop (restart recovery, cost guardrail, auto-purge, label triggers) for an operator who wants a passive manager. The GitHub App check behind the `github` field of `GET /repos` is separate and keeps running (see below). Only `on` and `off` are accepted. See Operations loop. |
| `FACTORY_MANAGER_LEASE_WAIT_SECONDS` | `120` | How long restart recovery waits for a repository's lease to become free before it gives up on that repository (0 to 3600; 0 probes once). |
| `FACTORY_MANAGER_POLL_SECONDS` | `60` | Interval of the cost guardrail (1 to 3600). Each pass runs one `status --json` per budgeted run. |
| `FACTORY_MANAGER_JANITOR_SECONDS` | `900` | Interval of the auto-purge sweep (1 to 86400). The first sweep is one interval after start. |
| `FACTORY_MANAGER_TRIGGER_SECONDS` | `60` | Interval of the label watcher (0 to 86400); **`0` turns the watcher off for every repository**. The trigger loop exists whenever this is above 0 (each pass re-reads the registered repositories, so a repository registered later is watched from the next pass, with no restart). The first pass is one interval after start (and after restart recovery). See "Label-triggered builds". |
| `FACTORY_MANAGER_AUTH` | `easy-auth` | `easy-auth` (identity from the `X-MS-CLIENT-PRINCIPAL` header set by the platform) or `entra-jwt` (identity from a verified `Authorization: Bearer` Entra token; the header is ignored). Anything else exits 2. There is no mode without authentication. |
| `FACTORY_ENTRA_TENANT_ID` | required for `entra-jwt` | The Entra tenant GUID (`<<TENANT_ID>>`). Tokens must carry this `tid` and the issuer `https://login.microsoftonline.com/<tenant>/v2.0`; the signing keys are fetched from `https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`. A domain name is refused. Ignored in `easy-auth` mode. |
| `FACTORY_ENTRA_SPA_CLIENT_ID` | derived | Optional, `entra-jwt` only: the client id (GUID) of the app registration the browser page signs in with ("Browser sign-in"). Unset, it is the first client id found in `FACTORY_ENTRA_AUDIENCES` (a bare GUID or `api://<GUID>`); with neither, the page is told sign-in is "unconfigured". A bad value stops the manager at start with a message that names the variable and not the value. |
| `FACTORY_ENTRA_SPA_SCOPE` | derived | Optional, `entra-jwt` only: the one scope the page asks for. Unset, it is `<client id>/.default` when the page's own registration is the API (its `api://<client id>` is among the audiences: Entra refuses a token request *for itself* by application id URI, AADSTS90009), else the first other `api://` audience as written, else `<client id>/.default`. `.default` puts the user's app roles in the `roles` claim without exposing a scope of your own. |
| `FACTORY_ENTRA_AUDIENCES` | required for `entra-jwt` | Comma-separated accepted `aud` values: the API's client id GUID (`<<CLIENT_ID>>`) and/or its application id URI (`api://<<CLIENT_ID>>`). GUIDs are compared in lower case. Ignored in `easy-auth` mode. |
| `FACTORY_HISTORY_MAX_BYTES` | `67108864` (64 MiB) | Upper bound of the history ledger's size on disk (1 MiB to 8 GiB, a whole number of bytes). The oldest segments go first. A bad value exits 2 naming the variable; the CLI, which shares the setting, falls back to the default instead of failing. See "History ledger". |
| `FACTORY_HISTORY_RETENTION_DAYS` | `365` | Days a ledger segment is kept after its last entry (1 to 3650). See "History ledger". |
| `FACTORY_MANAGER_METRICS_PORT` | unset (off) | Port of the separate metrics listener, which answers only `GET /metrics` (`docs/metrics.md`). Unset, empty or `off` means no listener; `0` picks a free port. It may not equal `FACTORY_MANAGER_PORT` (exit 2 naming the variable), and a port that cannot be bound exits 2. See "Metrics listener". |
| `FACTORY_MANAGER_METRICS_HOST` | `127.0.0.1` | The address the metrics listener binds. Anything but loopback needs `FACTORY_MANAGER_METRICS_TOKEN`: the manager exits 2 before listening on anything without it. |
| `FACTORY_MANAGER_METRICS_TOKEN` | unset | Bearer token of the metrics listener (32 to 512 printable characters, no spaces). Set, `GET /metrics` answers `401` without `Authorization: Bearer <token>`. A secret: never logged, never in a `repr`, not passed to any child process. See "Metrics listener". |
| `FACTORY_MAX_TOTAL_WORKERS` | derived from the container's cpu and memory | Pod-wide worker budget shared by all supervisors (inherited by the CLI processes the manager starts). Positive integer to set it, `0` or `off` for no limit. See `docs/capacity.md`. |
| `FACTORY_WORKER_MEMORY_MB` | derived (four fair shares of the pod's memory) | Per-session memory limit in MiB for the memory guard; `0` or `off` for no limit. See `docs/capacity.md`. |
| `FACTORY_CAPACITY_DIR` | `${FACTORY_DATA}/.capacity` | Slot files and waiting tickets of the worker budget; must be on the data disk shared by the supervisors. |
| `FACTORY_CAPACITY_WAIT_SECONDS` | `900` | How long a `plan` or `preflight` waits for a worker slot before failing. |
| `FACTORY_POD_BACKEND` | unset (off) | The manager-wide switch of the pod backend (`docs/pod-backend.md`). Only the exact value `on` is on; unset, `off` and anything else is off. The manager reads it at start and gives every supervisor the canonical word. A repository with `execution = "pod"` is **refused** (fixed text, nothing started, no fallback to in-process) while it is not `on` and, with it on, whenever the pod backend cannot be built for the session. With it on and everything configured the sessions run as pods (slice 12b: nothing starts the launcher or the relay yet, so no deployment can). Terraform sets it to `on` only when it enables the backend, which no deployment does yet. |
| `FACTORY_MANAGER_INSTANCE` | set by the manager | **Not a setting.** The manager draws 12 random lowercase hex characters at every start of its process (`podcontract.new_instance_id`), keeps them in memory and sets them in the environment of every supervisor and command it starts; a value already present in the inherited environment is replaced, so a restart always has a new one. It is meant to label the pods a manager launches (a pod from an older id was launched by an earlier process); nothing reads it yet. |

Not capped: the number of open connections. Each costs one thread for at most 20 s (headers) or 30 s (body); in `easy-auth` mode the Easy Auth proxy in front is what keeps unauthenticated peers away, and the manager must never be reachable without it; in `entra-jwt` mode every request except the pod's own `/healthz` probe and the static files of the browser page and its sign-in settings (which carry no data) is authenticated by the manager itself, but a peer still holds a thread for up to 20 s while it sends its headers, so keep the ingress source ranges as narrow as you can.

The manager's own environment is passed to every CLI process it starts (the
supervisor needs `ANTHROPIC_API_KEY`, the GitHub App settings and the lock settings:
`FACTORY_LOCK_ACCOUNT_URL`, `FACTORY_LOCK_CONTAINER`, `FACTORY_LOCK_CLIENT_ID`), apart from the
metrics token, plus the two names above that the manager itself sets: `FACTORY_MANAGER_INSTANCE`
and the canonical `FACTORY_POD_BACKEND`. The
manager never writes any of it to its own output. `main()` sets `umask 077`.

Output: one startup line, then one `audit` line per request. Nothing else is routine,
except the fixed-format lines of the operations loop (`restart`, `resume`, `budget`,
`purge`, `purge-clone`; see Operations loop), which carry only ids, statuses and numbers,
and, with a GitHub App configured, one `github repo=<org/name> status=<value>` line each time a
repository's GitHub check changes its result (see `GET /repos`).

```
factory-manager listening host=127.0.0.1 port=8080 repos=2
```

Run locally in `easy-auth` mode with a fake principal (the header is only honoured as sent;
there is no switch that disables the check, so a local operator supplies the header themselves):

```sh
export FACTORY_CONFIG=/etc/factory FACTORY_DATA=/var/lib/factory
factory-manager &
PRINCIPAL=$(printf '%s' '{"auth_typ":"aad","claims":[{"typ":"oid","val":"<<OBJECT_ID>>"},{"typ":"roles","val":"Factory.Operator"}],"role_typ":"roles"}' | base64 -w0)
curl -s http://127.0.0.1:8080/healthz
curl -s -H "X-MS-CLIENT-PRINCIPAL: $PRINCIPAL" http://127.0.0.1:8080/repos
curl -s -H "X-MS-CLIENT-PRINCIPAL: $PRINCIPAL" http://127.0.0.1:8080/repos/<<ORG>>/<<REPO>>/runs/r1/status
```

Run in `entra-jwt` mode (the AKS deployment) with a real token:

```sh
export FACTORY_MANAGER_AUTH=entra-jwt FACTORY_ENTRA_TENANT_ID=<<TENANT_ID>> FACTORY_ENTRA_AUDIENCES=<<CLIENT_ID>>,api://<<CLIENT_ID>>
factory-manager &
TOKEN=$(az account get-access-token --scope api://<<CLIENT_ID>>/.default --query accessToken -o tsv)  # or any OAuth2 client
curl -s http://127.0.0.1:8080/healthz                                   # loopback: no token needed
curl -s -H "Authorization: Bearer $TOKEN" https://<<MANAGER_HOST>>/repos
```

## Trust model

The manager has two authentication modes, chosen once at start by `FACTORY_MANAGER_AUTH`. In
both, everything except `/healthz` (see below for `entra-jwt`) and the static shell of the browser page (`GET /ui/`, its assets and `/ui/config.json`: no data, no secret; "The browser page") needs an authenticated identity and the same roles
(`Factory.Operator`, `Factory.Viewer`) with the same meaning; only where the identity comes from differs.

### `easy-auth` (default)

* **Identity** is the `X-MS-CLIENT-PRINCIPAL` header: base64 of JSON
  `{"auth_typ", "claims": [{"typ", "val"}, ...], "name_typ", "role_typ"}`. Easy Auth
  overwrites this header on every request that passes through it, so a caller cannot
  supply their own. A missing or malformed header (not base64, not JSON, no `claims`
  list, a claim that is not `{typ: str, val: str}`, larger than 64 KiB) is `401`.
* **Roles** are claims whose `typ` is the principal's `role_typ`, `roles`, or
  `http://schemas.microsoft.com/ws/2008/06/identity/claims/role`. Matching is exact and
  case-sensitive. A `role_typ` equal to the principal's `name_typ` (or to an object-id
  claim type) is ignored, so a user cannot gain a role by choosing a display name.
  * `Factory.Operator`: every `POST`, and `GET`.
  * `Factory.Viewer` or `Factory.Operator`: `GET`.
  * A valid principal with neither role: `403` for everything except `/healthz`.
* **Order of checks** (the same in `entra-jwt` mode, where `401` also covers invalid tokens, `503` means the
  signing keys are unobtainable, and `/healthz` needs no auth only for loopback peers):
  `/healthz` and the exact `GET` paths of the browser page's shell (no auth) -> `401` -> `403` (no role) -> `404` (unknown
  path) -> `405` (wrong method) -> `403` (viewer on a `POST`) -> `404` (repo not in the
  allowlist) -> `400` (bad run name) -> body checks (`413`, `400`). An anonymous caller
  therefore learns nothing about which paths exist.
* **Where the header can be trusted.** Only requests that went through Easy Auth carry a
  trustworthy header. Anything that can open a TCP connection to the manager's port
  without passing through the ingress can forge it. Keep the port reachable only through
  the ingress (no other app in the same environment, no published port) and treat this as
  an infrastructure requirement of the Terraform module. Run it on `127.0.0.1` locally.
* The `Authorization` header is not read in this mode.

### `entra-jwt` (AKS, no Easy Auth)

The manager sits behind a plain TLS-terminating reverse proxy and checks the caller's Entra
access token itself (`src/factory/entra_jwt.py`, standard library only).

* **Identity** is `Authorization: Bearer <jwt>` (scheme case-insensitive, exactly one space, no
  other whitespace). `X-MS-CLIENT-PRINCIPAL` is **ignored completely** in this mode: it is not
  parsed, not logged, and a valid one next to a missing, invalid or weaker token changes nothing.
  The proxy should still strip it, but the manager does not rely on that.
* **Token checks**, in this order. Anything that fails is the same `401`.
  1. Three canonical base64url segments, ASCII, at most 16 KiB; header and payload are JSON
     objects without duplicate keys or `NaN`/`Infinity`.
  2. `alg` is exactly `RS256` (`none`, `HS*`, `PS*`, `ES*`, other spellings are refused before any key
     is looked up); `kid` is present; `crit` is absent. `jku`, `x5u`, `jwk` and `x5c` in the header are never used.
  3. The signature verifies with the tenant's key for that `kid`. RSASSA-PKCS1-v1_5 / SHA-256 is computed
     directly: the signature must be exactly as long as the modulus and smaller than it, and the full encoded
     message recovered with the public key is compared in constant time with
     `00 01 FF..FF 00 <DigestInfo(SHA-256, hash of the signing input)>`. There is no padding parser, so no
     lenient encodings (missing NULL parameters, trailing garbage, short padding) are accepted. Moduli
     under 2048 bits and even or oversized exponents are not accepted from the key set.
  4. Only after that is the payload read: `iss` equals `https://login.microsoftonline.com/<tenant>/v2.0`
     (so the API's app registration must issue v2 tokens, `accessTokenAcceptedVersion: 2`; v1 tokens carry an
     `sts.windows.net` issuer and are refused), `aud` is a string equal to one of `FACTORY_ENTRA_AUDIENCES`,
     `tid` equals the tenant, `exp` is present and `nbf` (if present) is valid, both with 60 s of leeway.
* **Roles** are the strings in the `roles` claim (app roles of the API's app registration, assigned to
  users, groups or service principals). Matching is exact and case-sensitive, as in `easy-auth` mode. A valid
  token without `Factory.Operator` or `Factory.Viewer` (or with no `roles` claim) is `403`. `scp` and `groups`
  are not used.
* **Principal name** (audit line): `preferred_username`, else `azp`, else `oid`, else `unknown`.
* **Signing keys.** Fetched with the standard library over HTTPS (5 s timeout, 1 MiB cap, no redirects) and
  cached in memory.
  * The cache is refreshed when a token arrives more than 1 h after the last successful fetch, and when a
    token names an unknown `kid` (key rollover). No fetch, successful or not, starts less than 60 s after the
    previous one, so a stream of random `kid`s cannot make the manager hammer the identity endpoint. A refresh
    replaces the whole key set, so withdrawn keys disappear.
  * **Fail closed.** If the keys cannot be obtained (endpoint down, empty or unusable answer), a token
    whose key is not cached cannot be verified: the answer is `503` (`Retry-After: 5`), never an acceptance.
    Keys already cached keep verifying their tokens during an outage for up to 24 h after the last good
    fetch; after that every token is `503` until a fetch succeeds. A token that is wrong whatever the keys
    say (bad shape, wrong `alg`, no token) stays `401` during an outage.
  * A token that names an unknown `kid` within 60 s of a successful fetch is `401` (the tenant has not
    published that key); the client acquires a new token and retries. A failed fetch is reported on stderr as
    `factory-manager: signing key refresh failed (<exception type>)`, never with the URL, host or response.
  * The manager needs outbound HTTPS to `login.microsoftonline.com` (on AKS, through the cluster's NAT egress).
* **Errors are uniform.** A missing header, a malformed header, and every kind of bad token (signature,
  `alg`, `iss`, `aud`, `tid`, expiry, `nbf`, unknown `kid`, ...) give the same `401
  {"error":"authentication required"}` with `WWW-Authenticate: Bearer`; the response and the audit line never
  say which check failed. Tokens, signatures and claim values are never written to stdout or stderr.
* **`/healthz`** is unauthenticated **only for loopback peers** (`127.0.0.0/8`, `::1`, `::ffff:127.x`), judged by the TCP
  peer address and never by `X-Forwarded-For` or any other header. Any other peer, including the proxy and the
  public URL, needs a valid token (any role) and gets `401` or `503` like every other path. The Kubernetes
  probes therefore run inside the pod: `exec` of
  `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"`.
  Anything that can run code in the pod, such as the workers, can reach loopback; `/healthz` is the only
  thing that exempts a peer (the browser page's static files and its sign-in settings are served to anyone, whatever the peer, and carry no data), and it reveals nothing and changes nothing but reaping finished children.
* **Where this is safe.** The manager may be exposed to untrusted networks; each request still needs a token
  signed by the tenant's key for the configured audience. Behind a proxy that connects from the pod network
  (not loopback) nothing can reach the loopback exemption from outside. Never run the manager with
  `hostNetwork`, and never put a proxy that connects over loopback (a sidecar) in front of it in this mode.
* **Not covered:** token revocation before `exp` (tokens live up to about an hour; remove the role assignment
  and wait), and proof-of-possession tokens.

### Both modes

* The audit log records who did what; the history ledger adds the same principal to each successful operator action, so the
  answer survives the log's retention (`entra-jwt`: the verified identity, never a header; "History ledger").
  The audit log is the only record of a request that did not succeed, and of every read. Principal identifiers are logged
  (`easy-auth`: object id, else name, else `unknown`; `entra-jwt`: see above; `anonymous` when
  authentication failed); claims, tokens and the rest of the principal are not.

## Endpoints

`{o}/{r}` is the repository (`org/name`), `{run}` the run slug (`[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}`,
the same rule the CLI applies). Path parameters are percent-decoded once and checked by
regular expression before any filesystem or argv use. Every body is optional unless a
field is listed as required; unknown fields are `400`. In the argv column `CLI` is
`FACTORY_MANAGER_CLI` and `G` is `--repo {o}/{r} --run-dir <run dir>`; global options
come before the subcommand.

| Method and path | Role | Body | argv (after `CLI G`) | Success |
|---|---|---|---|---|
| `GET /healthz` | none (`entra-jwt`: none for loopback peers only, otherwise a valid token) | | | `200 {"status":"ok"}` |
| `GET /repos` | Viewer | | | `200 {"trigger_paused":false,"breaker":null,"repos":[...]}` |
| `GET /ui/`, `GET /ui/app.js`, `GET /ui/style.css`, `GET /ui/vendor/msal-browser.min.js`, `GET /ui/config.json` | none (the static shell of the browser page and its sign-in settings; "The browser page") | | none: no CLI runs | `200` the file or the settings; `GET /ui` is `307` to `/ui/` |
| `GET /dashboard` | Viewer | | none: no CLI runs | `200` the active overview: per repository the supervisor and the jobs in flight |
| `GET /history/runs` | Viewer | | none: no CLI runs | `200` past runs from the history ledger, purged ones included |
| `GET /history/jobs` | Viewer | | none: no CLI runs | `200` past jobs from the history ledger, purged ones included |
| `GET /repos/{o}/{r}/runs/{run}/jobs/{job}` | Viewer | | none: no CLI runs | `200` one job, from the live run or else the ledger; `404` when neither knows it |
| `GET /repos/{o}/{r}/runs/{run}/logs` and `.../jobs/{job}/logs` | **Operator** | | none: no CLI runs | `200` the allow-listed log files of the run or the job with their sizes; `403` for a Viewer |
| `GET /repos/{o}/{r}/runs/{run}/logs/{file}` and `.../jobs/{job}/logs/{file}` | **Operator** | `offset`, `limit` or `tail`, `before` | none: no CLI runs | `200` a bounded window of one log as `text/plain`, redacted; `404` for a name outside the allow-list, a missing file or a link; `403` for a Viewer |
| `POST /trigger-pause` | Operator | `paused` (`true` or `false`) required | none: no CLI runs | `200 {"trigger_paused":true}` |
| `POST /spend-breaker` | Operator | `clear` (`true`) required | none: no CLI runs | `200 {"breaker":null,"trigger_paused":false}` |
| `POST /repos/{o}/{r}/runs/{run}/plan` | Operator | `limit` 1-500 or `issues` (1-100 issue numbers; not both), `timeout` 1-86400, `budgets.plan` | `plan [--limit N] [--timeout N] [--issues=N,N,...] [--budget-plan X]` | `202 {"pid","started","kind":"plan"}` |
| `POST .../run` | Operator | `concurrency`, `verification_concurrency`, `review_concurrency`, `max_active_agents` (each 1-64), `timeout` 1-86400, `review_rounds` 0-3, `budgets.{build,review,verify}` | `run [--concurrency N] [--verification-concurrency N] [--review-concurrency N] [--max-active-agents N] [--timeout N] [--review-rounds N] [--budget-build X] [--budget-review X] [--budget-verify X]` | `202 {"pid","started","kind":"run"}` |
| `GET .../status` | Viewer | | `status --json` | `200` |
| `POST .../drain` | Operator | | `drain` | `200` |
| `POST .../pause` | Operator | | SIGINT, wait, then `status --json` | `200` |
| `POST .../resume` | Operator | `job`, `phase` required; `reason` | `resume JOB --phase P [--reason=R]` | `200` |
| `POST .../retry` | Operator | `job` required; `reason` | `retry JOB [--reason=R]` | `200` |
| `POST .../stop` | Operator | `job` required; `reason` (at most 200 characters); JSON content type | `stop JOB --json [--reason=R] --principal=P` | `200` with `"stop"`; `404` unknown job, `409` not running ("Stopping a job") |
| `POST .../preflight` | Operator | `job`, `phase` required | `preflight JOB --phase P` | `200` |
| `POST .../record-capabilities` | Operator | the file: a JSON array of objects | `record-capabilities --file <run>/inbox/<sha256>.json` | `200` |
| `POST .../record-blocker` | Operator | the file: a JSON object | `record-blocker --file <run>/inbox/<sha256>.json` | `200` |
| `POST .../import-evidence` | Operator | the file: a JSON object with a `job_id` | `import-evidence JOB --file <run>/inbox/<sha256>.json` (`JOB` is the body's `job_id`) | `200` |
| `POST .../purge` | Operator | | `purge` | `200` |

Field rules (all strict, types are checked, `true` is not a number):

* `job`: `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.
* `phase` for `resume`: `review`, `verify` or `publish` (the CLI's `--phase` choices).
  For `preflight`: `build` or `verify`.
* `reason`: a string of at most 2000 characters, no NUL, valid Unicode. It goes to argv as
  the single element `--reason=<text>`, so a reason that starts with `-` can never be read
  as an option. Argv is always a list; no shell is involved.
* `reason` for `stop`: at most 200 characters, stored as sanitised data ("Stopping a job"); the CLI also takes `--principal=<identity>`, the
  authenticated principal the manager passes.
* `issues` (`plan`): a list of 1 to 100 distinct positive integers (duplicates are dropped, the first
  occurrence keeps its place); the plan then covers exactly these issues, in this order, instead of
  "every open, non-ignored issue, newest first, up to `limit`". It cannot be combined with `limit` (`400`).
  A listed issue that is closed, missing, a pull request or labelled `factory-ignore` is dropped by the
  CLI with one line `factory: plan issue=<n> dropped reason=<closed|missing|not-an-issue|ignored>` in
  `plan.log`; its title and body are never fetched. If none is left, the plan fails.
* `limit`, `timeout`, concurrency and `review_rounds` are integers in the ranges above.
  `budgets` is an object whose values are positive, finite numbers of dollars and whose
  keys are the phases listed for that endpoint.
* Omitted fields are omitted from argv, so the CLI's defaults apply.
* JSON must be UTF-8, with no duplicate keys and no `NaN`/`Infinity`. Bodies are read with
  a `Content-Length` (chunked transfer is `411`); the cap is 64 KiB, 1 MiB for the three
  file endpoints (`413`, checked before the body is read).

### Short commands (status, drain, resume, retry, preflight, record-*, import-evidence, purge)

Run synchronously with `FACTORY_MANAGER_COMMAND_TIMEOUT`, in their own session, with the
manager's environment, at most `FACTORY_MANAGER_MAX_STATUS_COMMANDS` (`status`) and
`FACTORY_MANAGER_MAX_COMMANDS` (the rest) at a time. Every `status` starts a CLI process that asks
GitHub for the state of each open pull request with a freshly minted App token, so poll it no more than
every few seconds: the cap bounds memory, it does not cache. The response is

```json
{"returncode": 0, "output": "<CLI stdout>", "error": "<CLI stderr>"}
```

* `200` when the CLI exits 0, `422` when it exits non-zero (the command ran and refused;
  the streams say why), `504` when it timed out (its process group was killed).
* `status` additionally carries `"status": <parsed JSON>` when stdout is valid JSON, and
  every `status` response carries `"manager": {"child": ..., "last_exit": ...}`: the
  plan/run process this manager started for that run, if any, and how the last one ended.
* Output beyond 4 MiB (stderr 64 KiB) is cut and `"truncated": true` is added.
* These streams are returned to the caller only. They are never written to the
  manager's stdout or stderr.

### Inbox files (record-capabilities, record-blocker, import-evidence)

The body is the file the CLI loads. It is stored at `<run>/inbox/<sha256-of-body>.json`
(file mode `0600`, directory `0700`, written to a temp file in the same directory, fsynced
and renamed; opened with `O_NOFOLLOW` relative to the directory descriptor), then the CLI
runs with `--file <that path>`. The run directory must already exist (`404 run not found`
otherwise). The file is kept: the CLI records the path and digest in the job's history, and
it is deleted with the run by `purge`. The body must parse as JSON of the type the CLI's
loader requires: an object for `record-blocker` and `import-evidence`, an array of objects
for `record-capabilities`. Content beyond that is the CLI's to validate.

### Plan fragments (jobs added to a running run)

A run's `inbox/` also carries **plan fragments**: jobs appended to the run while it is
running. Only the supervisor (`run`) changes `plan.json` and `state.json`, under its own
repository lease; a fragment is only a file dropped in the inbox. The supervisor side
ingests them; `factory plan --issues N,... --append-to <run>` (see **Appending with the
planner** below) is what writes them from the planner, and the label watcher starts that
command for newly labelled issues while a run is going (**Topping up a running run** under
"Label-triggered builds"); otherwise an operator drops them.

**Which files.** `<run>/inbox/fragment-<name>.json`, `<name>` matching
`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Write it atomically: a temp file in the same directory
under any other name (for example `.fragment-<name>.json.tmp`), then rename it. Every other
inbox file is ignored and left in place: the `<sha256>.json` evidence, blocker and capability
documents of `write_inbox`, and temp or partly written files, until a rename gives them a
fragment name.

**Format.** One JSON object, exactly these fields (any other field refuses the file):

```json
{"version": 1, "kind": "plan_fragment",
 "issues": [{"number": 7, "title": "...", "body": "..."}],
 "jobs": [{"id": "docs-7", "issues": [7], "objective": "...", "likely_paths": ["docs"],
           "acceptance": ["..."], "feature": false,
           "depends_on": ["fix-a"], "context_issues": []}],
 "dispositions": [{"issue": 8, "status": "blocked", "reason": "needs approval"}]}
```

A job has the planner's fields (`depends_on` and `context_issues` optional). Each `acceptance` item is a string (a `sandbox` criterion) or an object
`{"text", "tag": "sandbox"|"ci"|"operator", "checks": [...], "workflow": "...", "reason": "..."}` (`reason` only for `operator`; a reason that is too long, blank or on another tag is cut or dropped, never refused), validated
exactly like a plan's (see "Where an acceptance criterion is proven" in `docs/architecture.md`): an unknown
tag, an empty text or a `ci` tag without check or workflow names refuses the file as
`schema:acceptance`, without echoing any text. The tag says where a criterion is proven, never
whether; a `ci` criterion with no matching required check is reported as unproven. The fragment's
`issues` are exactly the issues its jobs own, each by one job, plus the issues of its
`dispositions`, with the text the workers will see; each job also gets a `job` disposition in
the plan.

**Dispositions** (optional field, absent means none). Issues the planner did not turn into a
job: `{issue, status, reason}` with `status` one of `blocked`, `covered`, `investigation`
(never `job`, which stays derived from the jobs). `reason` is planner prose derived from the
issue: at most `fragments.MAX_REASON` (500) characters, and never written to a log line (the
logs carry counts and fixed reasons only). A job's issues and a disposition's issues must not
overlap and together are the fragment's issues; a fragment may have **no jobs** if it has at
least one disposition. Like jobs they only append: a disposition for an issue already in the
plan is refused (`issue-collision`), and the merged plan still passes `validate_plan` (every
issue has exactly one disposition). The plan's `fragments` ledger entry lists the disposition
issues as `dispositions` when there are any. Jobs added this way carry
`"fragment": "<file name>"` in `plan.json`, and the plan keeps a `fragments` ledger of
`{name, sha256, jobs}` for every applied fragment.

**Validation.** Like a plan: schema and types, job id charset (`[a-z0-9][a-z0-9-]{0,59}`),
literal repository-relative paths, and `validate_plan` over the merged plan. A fragment may
only **append**: job ids and issue numbers must be new, `depends_on` may name existing or new
jobs but must not form a cycle (or name an unknown job), and no existing job, issue or
disposition is changed (a fragment that would change the artifact paths inferred for an
existing job is refused too). The file is opened relative to the inbox directory with
`O_NOFOLLOW`, must be a regular file owned by the supervisor's user and at most 128 KiB.
Every `depends_on` entry must itself match the job id charset, so no file content other than
an id can reach a log line.

**Caps.** A run holds at most 10 jobs (`Runner(max_jobs=...)`, default
`fragments.MAX_RUN_JOBS`): a fragment that would take it past the cap is refused whole. The
run-wide `run_budget_usd` already covers added jobs: their usage is part of the run's cost,
and while the run's cost (jobs plus planner) has reached the limit the supervisor does not
start the build of a job that came from a fragment; it waits with `budget: run_budget_usd
reached` (the manager's guardrail drains the run as before).

**When.** At the start of every scheduling tick of `run` (about once a second while work is
running, and before the run decides it is finished), unless the run is draining. At most 32
files per tick, in file name order, so several fragments in one tick apply in a
deterministic order; each is checked against the plan including the fragments applied before
it, and only a conflicting one is refused. The plan is written first (atomically), then the
job states; the live plan changes only after both, so a failed write is retried on the next
tick in the same process. The new jobs are queued and the pool schedules them under the
usual dependency, shared-path and capacity rules. The plan's digest pin (`plan.sha256`,
checked by `run`, `recover`, `resume`, `retry`, `block` and the other commands that load the
plan) moves with each ingestion: the new digest is written to `plan.sha256.next`, then the
plan, then `plan.sha256`, and the pin check accepts a plan that matches `plan.sha256.next`
(a crash between the two writes), so any other edit of the plan is still refused. Restart
safety: a fragment still in the inbox after a restart
is ingested then; one that was applied but not yet renamed is recognised by its digest in the
ledger and only renamed; a crash between the plan and the state write is healed when the
state is loaded (jobs with a `fragment` mark that the state lacks start queued). So a
fragment is applied exactly once.

**Labels.** Once the state holds the new jobs, every issue the fragment added gets the
`factory:planned` label at ingestion, through the run's fenced `IssueLabels` (no existing
issue is touched); the issues of dispositions get the label of their status as in `sync_plan`
(`blocked` and `investigation` become `factory:blocked`, `covered` none; `factory:planned` is only for jobs).
Like `sync_plan` this is best effort: a label failure never fails the ingestion.

**Quarantine.** A refused file is renamed `<file>.rejected` (a numbered variant if that
exists), never deleted and never partly applied; an applied one becomes `<file>.applied`.
If a rename fails the decision is `deferred` and the file is left alone until restart.

**Log lines.** One per decision, on the supervisor's stderr (`supervisor.log`), never with
file content:

```
factory: fragment name=<file> decision=<applied|already-applied|quarantined|deferred> jobs=<id,id|-> reason=<reason>
```

Reasons: `ok`, `replay`, `invalid-json`, `schema:<field>` (`fields`, `version`, `kind`,
`issues`, `jobs`, `job-fields`, `job-id`, `objective`, `feature`, `likely_paths`,
`acceptance`, `depends_on`, `context_issues`), `size`, `symlink`, `not-regular`, `owner`,
`unreadable`, `id-collision:<id>`, `issue-collision`, `issue-ownership`,
`unknown-dependency:<id>`, `cycle:<id>`, `job-cap:<total>><cap>`, `invalid-plan`,
`artifact-ownership`, `commit-failed`, `quarantine-failed`, `retire-failed`. Disposition
problems are `schema:dispositions` (not a list, an unknown field, a `job` or unknown status),
`schema:reason` (empty) and `schema:reason-length`; an issue that is both a job issue and a
disposition issue, or a duplicate, is `issue-ownership`.

#### Appending with the planner (`plan --append-to`)

```
factory --repo org/name plan --issues 12,15 --append-to <run>
```

Plans the listed issues for an existing, **started** run (`--run-dir`, if also given, must be
that run; without it the run is `<data>/repos/org/name/runs/<run>`). `--issues` is required;
`--limit` and `--use-snapshot` are refused. The planner gets the run's current jobs (id,
`likely_paths`, status from `state.json`, `depends_on`, and jobs of fragments still waiting in
the inbox as `pending`) so it can depend on unmerged jobs and avoid their paths. The result is
checked exactly as the supervisor will check it (the fragment parser and merge, against the plan
plus the waiting fragments; a result that would be quarantined is refused and nothing is written)
and written as `inbox/fragment-append-<UTC time>-<random>.json` (temp file in the inbox, fsync,
rename; names sort in append order). Jobs come from the planner's jobs, the planner's non-`job`
dispositions become the fragment's `dispositions` (reasons cut to 500 characters). `plan.json`,
`plan.sha256` and `state.json` are never touched: the running supervisor ingests the file on
its next tick.

**Locking.** The repository lease is **not** taken (the supervisor holds it, and only it edits
the plan and state). Appends take a per-run lock, `<run>/append.lock` (an exclusive `flock`, so
two appends to one run queue up and the second sees the first's fragment as waiting), and plan
in their own worktree `<run>/worktrees/planner-append` (branch `factory/<run>/planner-append`),
with the evidence in `<run>/appends/<name>/`; the run's `issues.json` is not overwritten.

**Refusals** (before anything runs: no clone, planner call or inbox write), each
`factory: append refused: <reason>` with a fixed reason: `run not found` (no run directory or
`plan.json`; nothing is created), `run not started` (no `state.json`), `run is draining` (a live
supervisor has a drain request), `run is at the job cap` (jobs plus waiting fragments reached
`fragments.MAX_RUN_JOBS`), `run budget reached` (see below). Issues that are missing, pull
requests, closed, labelled `factory-ignore`, or already in the run (or in a waiting fragment)
are dropped with the usual fixed line `factory: plan issue=<N> dropped reason=<...>`
(`in-run` is the new reason), and their title and body are never fetched or logged; when none
is left the append fails with `none of the listed issues can be planned`. Titles, bodies and
the planner's reasons never reach a log line.

**Cost.** Each append writes its own `planner-usage-<name>.json` in the run directory right
after the planner returns (so a rejected result still costs), never over `planner-usage.json`.
`Runner.run_cost`, `status` and the budget check sum job usage plus every `planner-usage*.json`.
The check against `run_budget_usd` happens before the planner call: when the run's total has
reached it the append is refused.

### plan and run (long-lived children)

1. The request is validated. `plan` is refused with `409 run already planned` if the run has
   a `plan.json` or `state.json`; `run` with `404 run has no plan` if there is no
   `plan.json` (so an unknown run never creates a directory).
2. If this manager already started a plan or run process for the **repository** that is
   still alive: `409` with the running `kind` and `run`.
3. The lease is probed (next section): busy `409 {"error":"lease held elsewhere"}`, backend
   unavailable `503 {"error":"lease backend unavailable"}`.
4. The process is started in its own session (`start_new_session`) with stdout and stderr
   appended to `<run>/plan.log` or `<run>/supervisor.log`, created `0600` with `O_NOFOLLOW`
   and tightened if it already existed. The logs live in the run directory, are private to
   it and are removed with it by `purge`. Response: `202 {"pid", "started", "kind"}`.

The label watcher's top-up starts a third kind, `append` (`plan --issues=N,... --append-to <run>`), through
`Manager.start_append`: it skips step 3 (the running supervisor holds the lease and an append takes none), is only
accepted while this manager has the supervisor child of that run (otherwise `409`) and while no other append is
running for the repository (`409`), and logs to `<run>/append.log`. Step 2 is unchanged for every other start: a plan or
run started over HTTP is still refused with `409` while an append is running.

The manager keeps a registry of these children keyed by `(repo, run)` (an append by `(repo, run, "append")`, so it
never replaces the supervisor's entry), reaps them as they
finish (on every health probe and every registry read) and remembers how the last ones
exited (`GET /repos`, `status`). The logs can be read by an Operator through the log endpoints ("Log endpoints",
below), as redacted plain text in bounded ranges; they were never served before the dashboard's third phase.

### pause

`pause` works only for a process this manager started on this host. It sends SIGINT to the
child's process group (the supervisor is the group leader; workers have their own groups
and are cleaned up by the supervisor's own interrupt handling), waits up to
`FACTORY_MANAGER_PAUSE_SECONDS`, then runs `status --json`. A job with a live worker ends `failed`
(`agent exited -15`: the supervisor terminated the worker on the way out); a job the supervisor was
killed before recording stays `running` and becomes `interrupted` at the next `recover`. Either needs
`retry`/`resume` and a new `run` (`docs/operations.md`, "Drain, pause, and what each leaves behind"). If the child is still alive
at the deadline the response is `504 {"exited": false, "pid": N}`; a second `pause`, and a
shutdown that follows, do not signal again (a second SIGINT would land inside the supervisor's own
interrupt cleanup); the shutdown still waits for it and SIGKILLs it after the grace period.
No such child: `409`.

### Stopping a job

`POST /repos/{o}/{r}/runs/{run}/stop` with `{"job": "fix-a", "reason": "optional text"}` (Operator only) stops **one running job** with
drain semantics: the worker's current step may finish, within a grace period, and nothing new starts for that job.

1. **The request.** The manager reads the run directory (no CLI, no GitHub) and refuses without touching anything: `404` for a run or job it
   does not have, `409 {"error":"job is not running","job_status":"pr_ready"}` for a job whose status is not `running`, and
   `409 {"error":"no supervisor is running this run","supervisor":"stale"}` when no live supervisor owns the run. Otherwise it runs
   `factory stop JOB --json [--reason=R] --principal=P`, which writes a request into the control record of the live supervisor invocation
   (`supervisor-control.json`, the file `drain` uses; nothing outside the supervisor signals a process or edits job state) and prints one JSON
   object, returned as `"stop"`: `{"job", "stop": "requested" | "already_requested", "requested_at", "requested_by", "grace_seconds"}`.
   Asking again while the request is pending changes nothing and returns the first answer (`already_requested`, the first `requested_at` and
   principal; a later reason is ignored). Once the job is `stopped` it is no longer running: `409`, and nothing changes.
2. **The supervisor** takes the request on its next tick (about a second). For a job with a worker it starts the **grace period**
   (`FACTORY_STOP_GRACE_SECONDS`, 1 to 3600, default 120 seconds; a value outside the range means the default). The job starts no further session,
   phase or attempt. If the step (one `claude -p` session) ends inside the grace period, the job becomes `stopped` at once, with the checkpoint it
   reached kept (`resume` can continue from it) and its worker never signalled. If it does not, the supervisor sends SIGTERM to the worker's process
   group and SIGKILL 5 seconds later, the two steps `run_agent` takes for a session that runs out of time (the group's descendants go with it). The job
   is `stopped`, not `failed`: no error, and its cost so far is kept. A job with no worker at that moment (publishing, or between two sessions) is
   waited for, not killed.
3. **A job that finishes first** (its pull request is published, it becomes `blocked` or `failed`) is left exactly as it finished: the stop is moot and
   the control record says `finished`. The same holds for a request that reaches the supervisor after the job ended.

The job is then `stopped` in `status`, `GET /dashboard` and the job detail (`stop`: who, when, the reason as plain text, whether the worker had to be
terminated). Its issue gets the `factory:blocked` label ("needs a human") and no comment: an operator did it on purpose. The history ledger gets the
job entry with `status: stopped` and `stopped_by` (the principal, never the reason), so the job's outcome is `stopped` and `GET /history/jobs` shows
who stopped it and when; the manager also writes the `operator` entry (`action: stop`, principal, job) like for any action, and the audit line names
the principal. No new metric family is needed: `factory_jobs_finished_total{outcome="stopped"}` and `factory_manager_requests_total{route="stop"}` exist
already. A run with a stopped job ends with exit code 2, like one with a failed job.

**Continuing a stopped job:** `retry JOB --reason "what changed"` (the usual rule: a reason is needed after the first attempt) queues a new attempt in
the retained worktree; `resume JOB --phase review|verify|publish` continues from a saved checkpoint. Then `run`. Either one drops the job's `stop`
record, so a continued job shows no stop note and can be stopped again (`recover` drops the record of a stop that was still pending when the supervisor died).

**What stop does not do.** It does not close or change the pull request, the branch or the worktree; it does not stop the other jobs of the run (`drain`
and `pause` are the run-level stops); it does not stop a run that is paused or drained (there is no live supervisor: `409`; a dead supervisor's running
jobs become `interrupted` by `recover`, not `stopped`); it does not stop a job that is not running (queued, or waiting for a slot); it never merges and
never closes an issue.

**The reason** is optional text of at most 200 characters (more is `400`). Control, format and separator characters (newlines, escape sequences,
bidirectional overrides) become spaces and runs of spaces collapse. It is stored as data, in the control record and in the job's `stop` in
`state.json` (both in the run directory, which `purge` removes), and the dashboard shows it as plain text. It is never put in a log line, the audit
line (`--reason=<N chars>`), the ledger, a label or an issue comment. The principal is the authenticated identity, clipped to 128 characters
(`cli` for the command run by hand).

**Browser requests.** The endpoint requires `Content-Type: application/json` (`415` otherwise) and refuses (`403 {"error":"cross-origin request refused"}`)
a request that a page of another origin sent: a `Sec-Fetch-Site` other than `same-origin` or `none`, or an `Origin` that is not the request's own
`Host`. `curl` and the CLI send neither header and pass. In `entra-jwt` mode the credential is a bearer header, never a cookie, so a foreign page has
nothing to ride on; in `easy-auth` mode a cookie session sits in front of the manager, and these rules are what keep a cross-origin form from reaching
the endpoint. The order of the refusals is the documented one: `401`, `403` (no role, or a Viewer), `404`, `405`, then these two, then the body.

### POST /trigger-pause

Sets (`{"paused": true}`) or clears (`{"paused": false}`) the **trigger pause** (below, "Trigger pause"). It is the one
manager-wide action, so it has no repository or run in its path; it is behind the same authentication as every other path
(`401` without a valid identity, `403` for a Viewer or a token without a role: only `Factory.Operator` may call it), it is
audited like every other request (`argv=["trigger-pause","on"]` or `["trigger-pause","off"]`), it runs no CLI command and it
touches no process. Setting an already set pause, or clearing one that is not set, is `200` as well. A marker that cannot be
written is `500 {"error":"could not change the trigger pause"}`.

### POST /spend-breaker

Clears the **stop-the-world breaker** (`docs/capacity.md`, "Stop-the-world breaker"): `{"clear": true}` removes the marker `spend-breaker`
and stamps the clear. Like `POST /trigger-pause` it is manager-wide, Operator only (`403` for a Viewer or a token without a role),
audited (`argv=["spend-breaker","clear"]`), runs no CLI command and touches no process, and clearing a breaker that is not set is `200`
too. `{"clear": false}` or any other body is `400`; a marker that cannot be removed is `500 {"error":"could not clear the breaker"}`.
The reply carries `trigger_paused`, which is still `true` when the manual pause is set. Nothing is resumed: a run the breaker drained is
started by a person.

### GET /repos

`trigger_paused` is `true` while the manual pause marker **or** the stop-the-world breaker's marker exists, and `breaker` is
`{"code", "at"}` while the breaker is tripped (`code` is one of the closed codes of `docs/metrics.md`, or `null` for a marker that could not be
read; it still counts as tripped), else `null`. A manager with the pod backend wired in (slice 20 of `docs/pod-backend.md`; none is yet) adds a top-level
`"worker_pods": {"live": <n>, "final": <n>, "unknown": <bool>}` (the labelled worker pods that may still run, the ones that are over, and whether the
launcher could not be asked); every other manager omits the key, which the deploy gate (`tools/infra_deploy_gate.py`) reads as "no pods to wait for". Example:

```json
{"trigger_paused": false, "repos": [{"repo": "org/name", "base_branch": "main", "github": "ok",
            "false_incomplete": {"state": "ok", "rate": 0.125, "sample": "ok", "...": "..."},
            "supervisor": {"kind": "run", "run": "r1", "pid": 123, "started": "...", "running": true},
            "recent": [{"kind": "run", "run": "r0", "pid": 99, "returncode": 0, "started": "...", "finished": "..."}],
            "runs": [{"run": "r1", "planned": true, "started": true}]}]}
```

One entry per `repos/<org>__<name>.toml` whose file name is a valid repository name. A
config that does not load is listed as `{"repo": "org/name", "error": "invalid configuration"}`
and its other endpoints answer `500` (it has no `github` field). `runs` are the plain directories under the repo's
`runs/` whose names are valid run slugs.

`github` says whether Factory can get a GitHub App installation token for the repository with the
full permission set a job asks for (`tools/token_broker.py`, `PERMISSIONS`). It is a fixed vocabulary,
never a message from GitHub or the broker, and never a secret:

| value | meaning |
|---|---|
| `ok` | the last check minted a token |
| `permission missing: actions, issues` | the installation has not granted those permissions (sorted, lowercase GitHub permission names). GitHub refuses the whole token request (HTTP 422), so **no job can run** for this repository until an organisation owner accepts them (`docs/operations.md`, "Roll-out order matters") |
| `unavailable: token broker failed` | no token could be minted for another reason (key, app id, installation id, GitHub unreachable, broker timeout). The check deliberately shows no detail; run the broker command with the same variables by hand to read its one-line diagnosis |
| `unavailable: no installation_id in the repo config` | App variables are set, but the repository's config names no `installation_id` |
| `unavailable: invalid GitHub App configuration` | some, but not all, of the `FACTORY_GITHUB_APP_*` / `FACTORY_TOKEN_BROKER` variables are set, or one is malformed |
| `unavailable: invalid repository configuration` / `unavailable: check failed` | the repo config did not load for the check / the check itself failed unexpectedly (one stderr line names the exception type) |
| `checking` | no result yet: the first pass after start has not reached the repository |
| `ambient` | no GitHub App is configured, so Factory uses the operator's own `gh` login and there is nothing to check |

The values come from a background check, never from the request: `GET /repos` mints nothing and waits
for nothing. `serve()` starts the check right after the listening line (independent of
`FACTORY_MANAGER_OPERATIONS`; only when a `FACTORY_GITHUB_APP_*` variable is set). It mints one token per
configured repository through the same broker command a job uses, once at start and then every 5 minutes,
and re-reads the repository list on each pass, so a permission accepted later turns the entry to `ok`
without a restart, and a repository registered later is covered by the next pass. Each change of a
repository's value is one stdout line, `github repo=org/name status=permission missing: actions`
(everything after `status=` is the value, spaces included; the first pass writes one line per
repository, later passes write only changes). The check's thread is a daemon that stops when shutdown
begins, so shutdown does not wait for it; an unexpected failure of a pass is one stderr line
`factory-manager: github health pass failed (<Type>)`. There is no fallback to a narrower
permission set: the check reports the problem; it never hides it.

`false_incomplete` is the repository's **false-incomplete rate**: of the pull requests Factory published as
incomplete or pending that a person merged in the window, the share merged with the very head commit Factory
published (docs/operations.md, "False-incomplete rate", has the definitions, the targets and how to read it). The
manager keeps it in memory; the janitor sweep rebuilds it from GitHub (read-only, in the janitor's own thread, at
most hourly per repository), so `GET /repos` never calls GitHub and a restart starts at `checking` until the first
sweep. It holds counts, times and fixed words only: never an issue, criterion or pull request text.

```json
{"state": "ok",
 "window": {"from": "2026-10-08T12:00:00Z", "to": "2026-11-07T12:00:00Z", "days": 30},
 "min_sample": 10,
 "counts": {"counted": 16, "merged_unchanged": 2, "merged_changed": 14, "closed_unmerged": 3, "unresolved": 1,
            "workflow_hold": 4, "uncoded": 0},
 "rate": 0.125,
 "sample": "ok",
 "truncated": false,
 "by_reason": {"real_failure": {"counted": 6, "merged_unchanged": 0, "merged_changed": 6, "rate": null, "sample": "below_minimum"},
               "ci_failed": {"...": "one entry for each code of reason_codes.ORDER, in that order"}}}
```

| field | meaning |
|---|---|
| `state` | `ok`; `stale` (the last refresh failed, the previous values are kept); `unavailable` with a fixed `reason` (`no GitHub App slug`, or `refresh failed` with no earlier value) and nothing else; `checking` (no result yet, nothing else is present) |
| `window` | the rolling window by merge time: `from`, `to` (UTC) and `days` (30) |
| `counts.counted` | merged Factory pull requests whose published verdict held them back, and that the required checks did not resolve, **without** the deliberate `workflow_hold` ones: the sample |
| `counts.merged_unchanged` / `merged_changed` | counted pull requests merged with the published head (**false-incomplete**) / merged on a later head (a person pushed after the verdict) |
| `counts.closed_unmerged` | Factory pull requests closed without merging in the window: counted apart, in no rate |
| `counts.unresolved` | merged Factory pull requests whose head has no Factory verdict and whose published commit is not within 5 commits below it (a long tail of later pushes, or a force-push that rewrote the commits): in no rate. Every unchanged merge is found, so a large number means the rate leans high |
| `counts.workflow_hold` | merged pull requests held for a person because they change workflow files (merged unchanged by design): left out of `counted` and `rate`, shown in their own `by_reason` row |
| `counts.uncoded` | counted ones whose gate status carries no `[code]` (published before reason codes); in the totals, in no `by_reason` entry |
| `rate` | `merged_unchanged / counted`, 4 decimals; **`null`** when `sample` is not `ok` |
| `sample` | `ok` (at least `min_sample` = 10 counted), `below_minimum` (the rate is withheld) or `empty` (nothing counted: no rate, no division) |
| `truncated` | `true` when the scan bounds were reached (500 closed pull requests listed or 100 merged ones examined): the oldest of the window may be missing |
| `by_reason` | for each code in `reason_codes.ORDER` (`real_failure`, `ci_failed`, `ci_pending`, `workflow_hold`, `env_noise`, `unverified_other`): `counted`, `merged_unchanged`, `merged_changed`, `rate` and `sample` with the same rules and the same minimum, applied to that code alone |

### Dashboard endpoints (read-only)

The four `GET` endpoints above (`src/factory/dashboard.py`) answer "what is Factory working on now, and what did it do
before". They are the data side of a dashboard, shown by the browser page at `/ui/` (next section), which only reads them: there is no stop button for a single job, and
**no endpoint returns a log** (the rule that `plan.log`, `supervisor.log` and the transcripts are not served is
unchanged: they can contain agent-authored text and private repository content).

Rules they all follow:

* **Authentication is the same as for every other path and comes first**: `401` without a valid identity, then `403`
  for a principal without `Factory.Viewer` or `Factory.Operator`, then `404` for an unknown path or an unknown
  repository, run or job, then `405` for any method but `GET` (with `Allow: GET`), then `400` for a bad run name, job name or
  query. A Viewer and an Operator see the same. Both authentication modes apply (`easy-auth`, `entra-jwt`).
* **They never start a process.** No `gh`, no CLI, no lease probe: they read `plan.json`, `state.json`, the
  `planner-usage*.json` files and `supervisor-control.json` of the run directories, and the history ledger. Each file is
  opened without following a symlink and is read only when it is a regular file of at most 8 MiB; one that cannot be read is a
  row with `"error"` (`plan unreadable`, `state unreadable`, `not planned`), never a `500`. The audit line of a read has
  `argv=[]` and, as always, no query string.
* **Pull request state is recorded, not looked up.** A job's `handoff_status` comes from `state.json` (a job with a
  pull request is `published`). Over it, `pr_observed` shows the last state a status read of the cost guardrail or the janitor
  found (these reads happen anyway) with its `observed_at` and `age_seconds`, so the reader sees how stale it is; the
  top-level `"pr_state": "recorded"` says the same for the whole answer. `fresh` says whether GitHub confirmed the word
  (`true`), or the live lookup failed and the word is only what `state.json` says (`false`; a job with a pull request is then
  `published`). It is decided per job: the lookup that failed is the one of that job (`live_refresh.failed` of the status
  read names it), so a sibling's failure does not turn a confirmed word into a guess. A failed lookup never replaces a word
  GitHub confirmed earlier, nor that word's `observed_at`: the reader sees the confirmed word with its real age. Opening the
  dashboard costs no GitHub call.
* **Free text is not passed through.** A job's `error`, the retry reasons, the blockers' text and the criteria are not
  returned. `next_action`, `waits` and `owner` are Factory's own wording, control-stripped and clipped. The one place a
  run's agent-authored text appears is the `review.findings` of the live job detail (at most 20, each clipped to 500
  characters): the text `GET .../status` already returns to the same readers inside `error`. A client must still treat every
  string as data and escape it when it shows it.
* **Everything is bounded** (see the table below), and answers are `Cache-Control: no-store`.
* **Query parameters are strict.** An unknown name, a repeated one, a malformed query, a query over 1024 characters or an
  invalid value is `400 {"error":"invalid query parameter","parameter":"<name>"}`, where the name is one of the endpoint's
  parameters, or `query` for the first four cases. The value is never echoed. `GET /dashboard` and the job detail take none.

**`GET /dashboard`** (cached for 5 seconds, so a polling page costs the disk once per interval):

```json
{"generated_at": "2026-11-08T12:00:00.000000Z", "trigger_paused": false, "pr_state": "recorded", "truncated": false,
 "active_window_seconds": 43200,
 "history": {"segments": 2, "bytes": 81234, "max_bytes": 67108864, "retention_days": 365, "truncated": false},
 "repos": [{"repo": "org/name", "supervisor": {"kind": "run", "run": "r1", "started": "...", "running": true},
            "jobs_by_status": {"running": 1, "queued": 2}, "runs_total": 3, "runs_inactive": 2,
            "runs": [{"run": "r1", "planned": true, "started": true,
                      "supervisor": {"state": "running", "managed": true, "started_at": "...", "finished_at": null,
                                     "drain_requested_at": null, "result": null},
                      "last_activity_at": "2026-11-08T11:55:12.000000Z",
                      "budget_usd": 25.0, "planner_cost_usd": 0.4, "cost_usd": 3.1, "observed_at": "...", "jobs_total": 3,
                      "jobs": [{"id": "fix-a", "issues": [1], "status": "running", "phase": "build", "active_worker": true,
                                "attempt": 1, "age_seconds": 640.2, "status_age_seconds": 600.0, "phase_age_seconds": 310.5,
                                "cost_usd": 1.3, "pr": null, "handoff_status": null, "reason_code": null,
                                "blockers": 0, "waits": [], "owner": "build worker",
                                "next_action": "Continue build", "pr_observed": null}]}]}]}
```

`cost_usd` is the run's cost so far including the planner, the number the cost guardrail compares with `budget_usd`
(`run_budget_usd`, `null` without one). `supervisor.state` is `running`, `draining`, `planning` (a `plan` child of this
manager), `idle` (no supervisor, or one that finished: `result` then says `drained`, `stopped` or `interrupted`) or `stale` (the
control record says running but the process is gone). It is read from the control record and the manager's own child
registry; unlike `status` it does not probe the run's lock files, so polling it cannot collide with a supervisor that is
starting. `managed` is true when the manager itself started the process. Without a process of the manager's own, a record that says
running counts only while a process with its id is alive (not a zombie) and, read from `/proc`, started no later than the
record's `started_at` (a minute of slack): a process that started later got a reused id (a container restart counts them from 1
again), and the record is `stale`. Where `/proc` is not available only the id is probed, which a reused id can fool:
`GET .../status` is authoritative. `truncated` is true when a cap (100 repositories, 500 run directories
scanned and 50 active runs per repository, 500 job rows) left something out; `runs_total` and `jobs_total` say how many there were.

**Only active runs are listed.** A run directory is in `runs` while its supervisor is live (`running`, `draining` or
`planning`: a supervisor that waits on CI can record nothing for hours) or while its last activity is less than
`active_window_seconds` (12 hours) before `generated_at`. Every other run directory is left out and counted in the
repository's `runs_inactive`: its jobs are not in `runs` nor in `jobs_by_status`, and the runs and jobs it had are in
`GET /history/runs` and `GET /history/jobs` (from the ledger) for as long as the ledger keeps them. `runs_total` still counts
every run directory found. Leaving a quiet run out is not a cut, so it never sets `truncated`. A run's last activity
(`last_activity_at`, a UTC stamp, or `null` when nothing tells) is the newest valid stamp it recorded: `updated_at`,
`status_started_at`, `phase_started_at` and `created_at` of every job of `state.json`, and `started_at`, `finished_at` and
`drain_requested_at` of the supervisor control record's current entry. A run that recorded none, such as a planned run
that never started, falls back to the newest modification time of the run directory, `plan.json`, `state.json` and
`supervisor-control.json` (looked at without following a symlink); so does a started run whose `state.json` cannot be read,
when those times are newer than its supervisor's stamps (its job stamps are unknown), so it stays listed with its error. The ledger is not activity: a
status read of the cost guardrail or the janitor writes `pr_observed` entries for runs that do nothing any more. A stamp is
never newer than the file that holds it, so a run whose directory and those three files are all older than the window, and
whose supervisor is not live, is quiet without its `state.json` being parsed: with hundreds of old run directories a poll
costs a few `lstat` calls and the small control record of each. Quiet runs take neither a slot of the 50 runs nor any of the
500 job rows. When more runs are active than the cap, the newest (by run name, as `auto-<UTC time>` sorts) are kept and get
the job rows first; `runs` is still listed oldest first.

**`GET /history/runs`** and **`GET /history/jobs`** list the ledger's runs and jobs (newest last event first), including every
one whose run directory has been purged. Parameters:

| parameter | meaning |
|---|---|
| `repo` | `org/name` |
| `run` | a run name (`/history/jobs` only) |
| `outcome` | `merged`, `closed`, `blocked`, `abandoned` or `stopped`: the outcome of the run, or of the job on `/history/jobs` |
| `since`, `until` | an ISO date (`2026-11-01`) or date and time (`2026-11-01T08:30:00Z`); compared with the time of the run's (or job's) last ledger entry. `since` is inclusive; `until` is exclusive, except that a bare date covers that whole day |
| `limit` | page size: 1 to 100 runs (default 25) or 1 to 200 jobs (default 50) |
| `cursor` | the `next_cursor` of the previous page (a keyset cursor, so entries written meanwhile do not shift the pages). A cursor of the other list is `400` |

A run has `repo`, `run`, `issues`, `outcome`, `outcomes` (a count per outcome), `planned_at`, `started_at`, `finished_at`,
`last_result`, `first_at`, `last_at`, `duration_seconds` (first to last entry), `invocations`, `planner_cost_usd`, `cost_usd`,
`purged_at`, `purge_reason` (`terminal`, `abandoned` or `manual`), `operators` (who did what: the last 20 successful operator
actions with `at`, `action`, `principal`, `job`, `http`), `jobs_total` and up to 25 `jobs`. A job has `issues`, `status` (the last
recorded), `outcome`, `phase`, `attempt`, `pr`, `handoff_status`, `observed_at` and `observed_fresh` (GitHub's last known word, when it was
asked and whether GitHub confirmed it: `false` means only `state.json`'s word is known, `null` that the entry did not say), `reason_code` (one of `reason_codes.ORDER`), `cost_usd` (cumulative), `active_seconds`, `duration_seconds`, `first_at`,
`last_at`, `purged` and `purge_reason`. Every answer carries `ledger` (`segments`, `bytes`, `max_bytes`, `retention_days` and
`truncated`: true when the read cap left older entries unread) and `next_cursor` (null on the last page).

Outcomes (`history.outcome_of`, a table-driven function): a job is `merged` or `closed` when its status or GitHub's last
word for its pull request says so (a pull request a person merged leaves the job `pr_ready` in `state.json`, which is why the
ledger keeps GitHub's word); a run purged before its jobs finished is `abandoned`; a job that is `failed`, `blocked` or
`needs_human_publish` and not purged is `blocked`; `stopped` is the single-job stop ("Stopping a job"; `stopped_by` and `stopped_at` name who and when). A job
that is still going has no outcome (`null`), and so has a run with such a job. A run's outcome is the least successful of its
jobs' (`stopped`, `abandoned`, `blocked`, `closed`, `merged`).

**`GET /repos/{o}/{r}/runs/{run}/jobs/{job}`** answers from the run directory while it exists (`"source": "state"`): `status`, `phase`,
`attempt`, `retries`, `pr`, `handoff_status`, `reason_code`, `commit`, the ages and `phase_seconds`, `cost_usd`, `owner`,
`next_action`, `waits`, `blockers` (a count), `review` (`passed`, `matches_head`, `findings`, `findings_total`),
`verification` (`complete`, `matches_head`) and `phase_events` (the 200 newest: `at`, `status`, `phase`, `attempt`,
`previous_phase`, `previous_phase_seconds`). When the ledger knows the job too it adds `history` (outcome, times, purge) and, when it
has one, `pr_observed`. Once the run was purged, or its state cannot be read, the same URL answers from the ledger alone
(`"source": "ledger"`): the last known status, outcome, pull request, cost and a `timeline` of the job's ledger entries (200 at most);
the review findings, errors and phase events went with the run directory and are not in the ledger.

| bound | value |
|---|---|
| bytes of a state or plan file read | 8 MiB |
| bytes of the ledger read per answer | 8 MiB (the newest; `ledger.truncated` says so) |
| `/dashboard` | 100 repositories, 500 run directories scanned and 50 active runs per repository (the newest), 500 job rows (`truncated`), reused for 5 s; runs with no activity for 12 hours and no live supervisor are left out (`runs_inactive`, not `truncated`) |
| `/history/runs` | 100 per page (25 default), 25 jobs shown per run (`jobs_truncated`) |
| `/history/jobs` | 200 per page (50 default) |
| job detail | 200 phase events, 20 findings of 500 characters |
| the folded ledger | kept in memory and read again only when a segment changed |

### Log endpoints (Operator only)

**The rule changed.** Until the dashboard issue's third phase the manager never returned a log: `plan.log`, `supervisor.log` and the per-job
transcripts and stderr can hold agent-authored text and private repository content, so nothing served them and inspecting a worker meant a
shell on the pod. Now an **Operator** can read them, and only an Operator: a `Factory.Viewer` token gets `403 {"error":"forbidden"}` on every
log path, and there is no new role. Everything below exists because the content is untrusted and sensitive.

| Path | Answer |
|---|---|
| `GET /repos/{o}/{r}/runs/{run}/logs` | `{"repo","run","files":[{"name","size","modified"}]}`: `plan.log`, `supervisor.log`, `planner.jsonl`, `planner.stderr` of the run, those that exist |
| `GET /repos/{o}/{r}/runs/{run}/jobs/{job}/logs` | the same for one job: `<attempt>-<phase>.jsonl` and `.stderr` (`1-build-0`, `2-review-1`, `1-verify`, `1-verify-baseline`, `preflight-build-<UTC stamp>`); `files: []` for a job with no logs yet |
| `GET .../runs/{run}/logs/{file}`, `GET .../jobs/{job}/logs/{file}` | one file as `text/plain; charset=utf-8` |

**What can be named.** An exact allow-list of file shapes (`logs.RUN_FILES`, `logs.JOB_FILE`). Settings (`*.settings.json`), prompts
(`*.prompt.txt`), system prompts, result JSON, `pr-body.md`, `state.json`, `append.log` and everything else are never opened and never listed; a
name outside the list is the same `404 not found` as a file that does not exist. The run and job names are matched against the same patterns as
everywhere else (`400 invalid run name` / `invalid job name`) before any path is built, and percent-encoded or dotted names match nothing.

**No traversal, no links.** The run directory is reached from `FACTORY_DATA` one component at a time with `openat` and `O_NOFOLLOW`, so a
symlink at any depth (the repository directory, `runs/<run>`, a job directory, the file) is refused. The file is opened with `O_NOFOLLOW` and
`O_NONBLOCK` (a FIFO cannot hang the server) and must be a regular file with exactly one link (a hard link to a file elsewhere is refused). A
worker cannot write these directories, but a log is the one thing the manager reads from a place a worker's output reaches, so the check does
not depend on that.

**Plain text only.** `Content-Type: text/plain; charset=utf-8`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none';
frame-ancestors 'none'; sandbox`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Content-Disposition: inline`. Nothing in the answer is
markup or is ever produced as markup, and the dashboard shows the text in a `<pre>` as a text node.

**Cleaned and redacted before a byte leaves the process** (`src/factory/redact.py`), on every line, so on every line of a JSONL transcript too:

* ANSI escape sequences (and their escaped spelling inside a JSON string), every other control character, line and paragraph separators,
  zero-width characters and bidirectional overrides are removed (first, so a token cannot be split by an escape sequence).
* Strings of these shapes become `[REDACTED]`: GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), Anthropic keys (`sk-ant-`),
  private key blocks (also across lines in a plain text log, see below; in a JSONL transcript a key can only be inside one line), the values of `Authorization`, `Proxy-Authorization`, `x-api-key` and `Bearer`, the values of names that
  end in token, secret, password, api key, private key, access key, account key or credentials, Azure shared access signatures (`sig=`),
  JWT-shaped strings (`eyJ...eyJ...`) and a line that is only a long base64 run (the body of a key seen without its first line).
* Numbers and hashes are left alone (`"input_tokens":1234` and a commit id stay readable).

**What the redaction cannot guarantee.** It matches shapes. A secret with an unusual shape, a short password in prose, a value a model
re-encoded or split over several lines (other than a key block), or a private repository's source code and customer data are shown as
they are. A window that begins inside a key block is protected only by the base64-line rule. The unfinished last line of a file that is
still being written is shown as far as it was written. So the Operator role is the real control; redaction is a second layer, and the
page says so.

**A key block that never ends is bounded.** In `plan.log`, `supervisor.log` and the stderr files a block opened by a `-----BEGIN ... PRIVATE KEY-----`
line drops the lines after it until the matching `END` line, but at most 200 lines or 16 KiB (a real key is under 110 lines): past that the
line is shown again, redacted like any other, with the number of dropped lines in front (`[200 lines withheld] ...`), and a window or file that
ends inside a block ends with `[N lines withheld]`. So a line that only mentions the marker (a `grep`, a test, a comment) cannot make the rest of a log vanish
without a trace. In a **JSONL transcript** every event is one line and a key cannot span lines, so nothing is carried from one line to the next: a start
marker with no end on its line is replaced up to the end of the JSON string that holds it, the line stays valid JSON and the rest of the transcript is
unaffected. The key-block search is linear in the length of the line (a line of a million start markers takes milliseconds).

**Bounded; never read whole.** `GET .../{file}` takes either `offset` (a byte offset) with an optional `limit` (1 to 262144 bytes, default 65536),
or `tail` (the last N bytes, 1 to 262144) with an optional `before` (read up to that offset), not both; with no parameter it is the **tail**
(65536 bytes). Windows are **line-aligned**: a start inside a line moves to the next line start and an end inside a line moves back, so a
secret inside a line is always seen whole. The body is at most 512 KiB of text (decoding invalid bytes can widen a window; whole lines are dropped
from the far side to fit), a request examines at most 2 MiB of the file to find a line boundary (`pread` in 64 KiB steps; the file is never read
whole, however large), a line over 32 KiB is cut back to a word boundary and says `[line cut: N bytes]`, and at most 4 log reads run at once
(another waits 2 s, then `503` with `Retry-After`). The range is in the headers:

| Header | Meaning |
|---|---|
| `X-Log-Size` | the file's size in bytes when it was read |
| `X-Log-Start`, `X-Log-End` | the bytes of the file this answer covers (`start` up to, not including, `end`); redaction changes the text's length, not these |
| `X-Log-More-Before`, `X-Log-More-After` | `true` when the file has bytes before `start` or after `end`: ask `tail=N&before=<start>` for earlier text and `offset=<end>` for later text |
| `X-Log-Partial` | `true` when the file ends in an unfinished line (no newline yet) that was **left out** because the file was written to in the last 3 seconds: `end` is then the start of that line, so `offset=<end>` returns the whole line once it is finished (issue 226; a file quiet for 3 seconds shows its unfinished last line, as before) |

Query errors (an unknown, repeated or invalid parameter, `offset` with `tail`, `limit` without `offset`) are `400 invalid query parameter` and the
value is never echoed.

**Audit.** The usual audit line for a request, with the byte count of the text returned and never any of it:
`audit principal=... method=GET path=.../logs/1-build-0.jsonl status=200 argv=[] bytes=65432`. A `403` or `404` has no `bytes`.

**Where a purged run's logs are.** The files go with the run directory; the retained copy ("Log retention") is not served by the API.

### Log retention (a copy before `purge`)

A merged run is purged within one janitor tick, so its logs would vanish almost at once. **`purge` first copies them** (`src/factory/logarchive.py`):
once every check has passed and right before the run directory is deleted, the CLI writes one **redacted, gzipped** file per allow-listed log:

* default: a directory of the data volume, `${FACTORY_DATA}/logs-archive/<org>__<name>/<run>/<file>.gz` and `.../<job>/<file>.gz` (at the top of the
  data directory, outside `repos/`, so `purge` and `purge-clone` leave it and a worker's sandbox hides it), directories `0700`, files `0600`;
* with `FACTORY_LOGS_ARCHIVE_ACCOUNT_URL`: the same keys in a container of the existing Azure storage account (the AKS deployment sets it; `infra/README.md`,
  "Retained logs"), by the Blob REST API (`urllib`, the pod's workload identity token, no proxy, no redirect), one `Put Blob` per file.

A file over 128 MiB keeps its tail, a line over 1 MiB is cut, a run stops at 512 MiB of raw logs, and the whole copy has a wall-clock budget of 150 s
(the manager kills a `purge` CLI at its 300 s command timeout, which would leave the run directory for ever): past it the file in progress is kept as far as
it got, with a last line saying so, the remaining files are not copied and the copy fails like any other (below). **Retention:** the janitor's sweep removes
copies whose modification time (blob `Last-Modified`) is older than `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS` (default 30), at most once a day (the first
sweep after a start runs at once), with a bound of 20 list pages and 2000 deletes per sweep; it logs `log-archive swept removed=<n> retention_days=<d>`
only when it removed something. A run archived again starts its clock over.

**Failure policy (decided): the purge goes on.** The copy is best effort. If it fails (the identity token, the network, the disk, an invalid
setting) the CLI prints one fixed line on stderr, `factory: log archive failed (<error type>); the logs go with the run`, and the purge deletes the run
anyway: a run directory kept for ever because storage is down would fill the data disk and keep the repository's clone from being released.
The manager then logs `log-archive failed repo=<r> run=<r> the logs went with the run` and counts it in `factory_log_archive_failures_total`
(a failed sweep is counted too, with `log-archive sweep failed error=<type>`). A success prints nothing, so `purge`'s output is unchanged.

An invalid `FACTORY_LOGS_ARCHIVE*` value stops the manager at start (exit 2, the variable is named). There is **no endpoint that reads a copy**:
an operator reads one from the volume (`kubectl exec`, `zcat`) or the container (`az storage blob download`, `docs/operations.md`, "Logs").

### The browser page (`/ui/`)

A single static page for operators, served by the manager from the image: `src/factory/ui/index.html`, `app.js`, `style.css` and the
vendored Microsoft sign-in library `vendor/msal-browser.min.js` (`src/factory/webui.py` serves them). There is **no build step and no
third-party script, style or font** (no CDN; the one library is a local file, hash-pinned, `src/factory/ui/vendor/README.md`), and the
files are package data (`pyproject.toml`), so they are in the wheel and in the image; `image/smoke.sh` checks that the installed package
loads them. The page is the view of the four endpoints above and the log endpoints, plus one read of `GET /repos` (the Active view only, one call after `/dashboard`, for the per-repository `false_incomplete` figure; a failure shows "not available"), plus one control, the stop of one running job (the **Stop** bullet below): **it shows, and
acts only on that confirmed request**. The job view has a **Logs** panel (below). With `FACTORY_MANAGER_AUTH=entra-jwt` the page signs in with Microsoft ("Browser sign-in");
in `easy-auth` and local mode the page offers a paste field, **but a pasted token gets no data there**: `easy-auth` mode reads only the `X-MS-CLIENT-PRINCIPAL` header and ignores `Authorization`, so in a plain local run the page loads and every data call answers `401`. The page works in `entra-jwt` mode, or behind an authenticating front that validates the bearer token and sets that header from it (`docs/operations.md`, "The dashboard in a browser").

| Request | Answer | Token |
|---|---|---|
| `GET /ui/` | the page, `text/html`, `Cache-Control: no-store` | none |
| `GET /ui/app.js`, `GET /ui/style.css`, `GET /ui/vendor/msal-browser.min.js` | the assets; `Cache-Control: public, max-age=31536000, immutable` only for the address the page links, `?v=<first 16 hex digits of the file's SHA-256>`, otherwise `no-cache` (revalidated every time) | none |
| `GET /ui/config.json` | the sign-in settings of the page (below), `application/json`, `Cache-Control: no-store`; generated from the manager's settings, never read from a file or a request | none |
| `GET /ui` | `307` to `/ui/` (a fixed, relative location, an empty body) | none |
| any other method on those paths | `401` / `403` / `405` with `Allow: GET`, in the usual order | as for every other path |
| any other path, including `/ui/index.html`, `/ui/app.js/`, `/ui/../healthz`, percent-encoded or dotted variants | the usual unknown-path answers: `401` without a token, `403` without a role, `404` | as for every other path |

**Why the shell is public.** A browser navigation cannot send an `Authorization` header, and the page must load before it can ask the
operator for a token, so the files are served without one. They carry no data and no secret: the same bytes for everyone, with the
asset versions in them. Every data call the page then makes is an ordinary API call with the bearer token: `401` without one, `403`
without a Factory role, exactly as in "Dashboard endpoints". The acceptance line "an unauthenticated request to any page returns 401"
is therefore read as "to any page or call that shows data". Nothing else became public: the exception is an exact-string allow-list
(`webui.SHELL`), compared with the raw request path, so there is no directory listing, no path is ever joined to a request and nothing
is read from a request. Each file is opened without following a symlink, must be a regular file of at most 512 KiB, and all four
are read once into memory; if any is missing or unusable none is served (`404`, retried on the next request). In `easy-auth` mode the
platform in front still authenticates browsers before a request reaches the manager; the shell is served the same way in both modes.
A shell request is audited like any other (`principal=anonymous`, the path without its query) and counted under the `ui` route label.

Every answer of the shell carries:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' https://login.microsoftonline.com; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`. The login origin is added to `connect-src` **only while Entra sign-in is on** (it is `webui.LOGIN_ORIGIN`, the constant the token verifier takes its issuer from, never a wildcard and never from a request); otherwise `connect-src 'self'`. No inline script or style, no `unsafe-*`, no framing, no form post, no base |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `X-Frame-Options` | `DENY` |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Cross-Origin-Opener-Policy` | `same-origin`. The sign-in is a redirect, not a popup, so no window needs an opener |
| `Permissions-Policy` | every feature the page does not use denied (`camera=()`, `microphone=()`, `geolocation=()`, `payment=()`, `usb=()`, ... the list is `webui.PERMISSIONS_POLICY`). The sign-in itself happens on Microsoft's own page, where this policy does not apply |

`Strict-Transport-Security: max-age=31536000` is added to **every answer of the shell and of the API, only when the request arrived over
HTTPS** (below), never with `includeSubDomains` or `preload`.

**How the manager knows a request was HTTPS.** Behind Caddy the manager itself speaks plain HTTP, so it can only be told. It believes
`X-Forwarded-Proto: https` (that exact value, any letter case) **only from a peer that is this host or on a private network**
(`127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fc00::/7`, `fe80::/10`, `::1`; the TCP peer, never a header), which is where Caddy
connects from in the cluster. From any other peer, or with any other value (`http`, a list), the request counts as plain HTTP and gets no
HSTS. Caddy replaces any `X-Forwarded-Proto` a client sent (it has no `trusted_proxies`, and a test guards that). Nothing but this header depends
on it, and a browser ignores HSTS received over a plain connection, so a forged value can do no more than make a response carry it
(`manager.forwarded_https`). A manager reached directly over HTTP gets no HSTS.

**The sign-in settings, `GET /ui/config.json`.** The page needs the tenant, the client id, the authority and the API scope to sign in. They
are not secrets (every sign-in URL carries them), so one small public, read-only route serves them, built from the manager's own settings
(`webui.sign_in_config`); a test pins the exact key set. `Content-Type: application/json`, `no-store`, the headers above. Its body is one of:

| `mode` | When | Keys |
|---|---|---|
| `entra` | `FACTORY_MANAGER_AUTH=entra-jwt` and a client id is known | `v` (1), `mode`, `tenant_id`, `client_id`, `authority` (`https://login.microsoftonline.com/<tenant>`), `scope` |
| `unconfigured` | `entra-jwt`, but no client id can be told (the audiences name only an `api://<name>` URI and `FACTORY_ENTRA_SPA_CLIENT_ID` is unset) | `v`, `mode`. The page says so and offers no way in |
| `token` | `easy-auth` (the default) and local mode | `v`, `mode`. The page says Microsoft sign-in is not configured here and offers a paste field; the pasted token travels as an `Authorization` header, which `easy-auth` mode ignores, so a plain local run answers every data call with `401` (it helps only behind a front that turns the bearer token into the `X-MS-CLIENT-PRINCIPAL` header) |

Nothing else is ever in it: no key, no token, no audience list, no request value. The page validates every key again (GUIDs, the exact
authority shape, a single scope with no space) and drops what does not fit; a config it cannot use leaves the page with a "sign-in not
available" message, never with a paste box.

How the page treats what it shows:

* **Data is text, never markup.** Issue titles, reasons, branch names and findings are agent- or user-authored. Every value is added
  with `createTextNode` through one element builder that accepts a short list of attributes and refuses handlers, `style` and any link
  that is not on the allow-list; no HTML-string API is used anywhere (a test greps the script for them, and another renders hostile data in a
  fake DOM). Status words become a CSS class only after they match `[a-z][a-z0-9_-]{0,31}`.
* **Links are built, not copied.** An issue link is `https://github.com/<owner>/<repo>/issues/<n>` from the repository name and the number; a pull
  request link is rebuilt from a URL that matches `https://github.com/<owner>/<repo>/pull/<n>` exactly and belongs to the same repository.
  Anything else is shown as plain text without a link. Links open in a new tab with `noopener noreferrer`.
* **The token and the sign-in** live behind one object with four calls (`getToken`, `signIn`, `signOut`, `signedIn`), so no view knows where
  the token comes from; the provider is chosen by `/ui/config.json`. **Entra mode:** MSAL (`@azure/msal-browser` 5.25.1), authorization code
  flow with PKCE, **redirect flow only** (no popup, no hidden frame: `allowRedirectInIframe` is off and the page's own frame policy would refuse
  one), cache in this tab's `sessionStorage`, MSAL's logger replaced by a function that does nothing. The redirect URI is the page itself,
  `<origin>/ui/`, chosen at run time from the address the page was loaded from, so each host needs its own registered URI (`infra/README.md`).
  The scope is the one from the config. A token is requested silently from the cache or with the refresh token (`CacheLookupPolicy`
  `AccessTokenAndRefreshToken`; `RefreshToken` to force a new one), which never uses a frame. **No token or code is ever in an address**: the
  response of Microsoft arrives in the fragment, MSAL reads it and the page replaces the address at once with `/ui/` and the validated hash route
  it saved before leaving (`factory.route`, only a string that matches `#/[A-Za-z0-9._/?=&%-]*`). The tab's storage holds MSAL's own cache and two non-secret
  markers (`factory.signin`, the time of the last sign-in redirect, and `factory.route`). **Token mode** (`easy-auth` and local mode only; the manager ignores `Authorization` in `easy-auth` mode, so the token is of use only behind a front that sets `X-MS-CLIENT-PRINCIPAL` from it, and a plain local run answers every data call with `401`): the token
  is pasted into a field (`type=password`) and kept in the tab's `sessionStorage` only (an in-memory copy when the storage is unavailable), never in
  a URL, a cookie or a log line. Whichever provider, the token is sent in one place, an `Authorization` header on a `fetch` to a path on the page's
  own origin (`credentials: omit`, `redirect: error`, no referrer).
* **A 401 never makes a loop.** The first `401` of a call gets one fresh token (a refresh-token request, never a frame) and one retry. If the manager
  still answers `401`, or only the user can renew the session (the refresh token is gone), the page starts **one** sign-in redirect, and refuses another
  automatic one for 5 minutes (`factory.signin`), so a token the manager keeps refusing ends in the "session expired" state with a button, not in a
  loop; polling stops on `401` and `403`. A `403` is final (a new token would not add a role). A network failure while renewing is a failed call, not a
  sign-in. Pasted tokens are tried once.
* **The Logs panel** (job view, live runs only): it lists the job's and the run's log files (`plan.log`, `supervisor.log`, `planner.*`; each job's
  `<attempt>-<phase>.jsonl` and `.stderr`) and shows one at a time in a `<pre>` as a text node, tail first, with
  *Load earlier* and *Load later* by range (the last 8 windows are kept), *Show the end again* and *Close*. File names are matched against the
  server's allow-list before a path is built. Over the **loaded windows only** (never the whole file: load more to search more) it has a
  **search** box (plain text, not a pattern, case-insensitive unless *Match case* is ticked; an empty search matches every line), which names the number of
  matching lines with their line numbers and byte positions (a position is the window's start byte plus the bytes before the line in the redacted text, so it is exact
  only where nothing was replaced) or says that nothing matched; *Only matching lines* then shows just those lines. **Wrap long lines** switches a CSS
  class on the `<pre>` and nothing else (no inline style). **Copy visible range** copies exactly the text shown, filtered or not, with
  `navigator.clipboard.writeText`, and answers in fixed words (*Copied the visible text.*, *Nothing to copy.*, *Copy failed: the browser did not allow it.*; the browser's own
  error is never shown). **Transcripts**: a `.jsonl` file (a job's `<attempt>-<phase>.jsonl`, the run's `planner.jsonl`) is shown as readable turns when at least one
  loaded line is stream-json the page knows (assistant and user text, thinking, tool calls, tool results, the final result); tool calls, tool results and thinking are folded
  in `<details>`, and a fold the reader opened stays open when the panel redraws. A line that cannot be parsed (empty, truncated, not an object, an unknown type) is shown as its
  raw redacted text; long strings are cut and control characters replaced. **Raw lines** shows the plain `<pre>` instead, and `plan.log`, `supervisor.log` and `*.stderr` always
  show plain text. The parser is a pure function (`parseTranscriptLine`) and builds only elements and text nodes. Log content reaches the page only through `textContent` and text nodes. A Viewer's token gets `403`, which the panel says plainly
  ("Logs can be read by operators only"). A `404` on the listing or on a file (a purged run or job) says the logs went with the run. The page still has one `fetch`, now also for `text/plain`.
  **Several files can be open at once** (up to four, side by side; one column on a narrow screen), each panel with its own windows, range, search state, scroll
  position and controls, a heading the keyboard can reach, and a polite live region that says how many lines arrived. Each panel shows from the
  `X-Log-More-Before` and `X-Log-More-After` headers whether more text exists before or after the loaded range; a value that is not exactly `true` or `false`, or that
  contradicts the range of the same answer, is shown as *unknown*. **Follow new lines** (on by default) polls the tail of the open file with
  `?offset=<last end>&limit=65536` (the existing range keys only), appends what arrives after the last loaded end, and backs off while the file is idle.
  New text that continues the last window is merged into it (until it spans 64 KiB), so a few lines per poll do not push the loaded tail out. When the last window ended inside a line that was still being written, the next answer starts past that end; the panel then reads the last window again from its own start and replaces it, so the rest of the line is shown.
  It pauses while the tab is hidden or the text is scrolled up from the bottom, and resumes by itself when the tab is shown or the text is scrolled back down.
  Log reads carry their own abort signal, so a panel's poll never cancels the dashboard poll or another panel's read.
* **Stop.** A running job has a **Stop** button on the active list and on its detail view. It opens a confirmation that says what will happen
  ("Stopping a job"), with an optional reason field (200 characters, plain text), and sends nothing before **Confirm stop**. A token that names its
  roles (the `roles` claim, read without verifying anything, only as a hint) without `Factory.Operator` sees the button disabled and the reason; when
  the manager refuses (`403` for a Viewer, `409`, `404`, `401`) the page says so in fixed words. A job that has a stop shows who asked, when and why,
  as text. The poll does not redraw an open confirmation.
* **No GitHub call per view.** The page only calls `/dashboard`, `/history/runs`, `/history/jobs` and the job detail, which read files
  (a test fails if a page view or any of those calls starts a process). Pull request state is what the run recorded; each job shows when it was
  last seen and whether GitHub confirmed it (`pr_observed`), and the header shows how old the data on screen is, turning to a warning after a
  minute.
* **Views** (hash routes, so no server support is needed): `#/active?q=&status=&repo=&idle=1&sort=name` (the Active board, the next bullets);
  `#/history?kind=runs|jobs&repo=&run=&outcome=&since=&until=&limit=` (the ledger, filtered and paged with the keyset cursor, 25, 50 or 100 per page,
  purged runs included); `#/job/<org>/<name>/<run>/<job>` (facts, phase timeline, attempts, time per phase, review findings, links to the issue and
  the pull request; a purged run's job is answered from the ledger and says so).
* **The Active board holds active runs only**: a run with a live supervisor or with activity in the last 12 hours (`active_window_seconds`, "Dashboard
  endpoints"). Older runs are counted per repository ("2 older runs with no activity in the last 12 hours are in History", with a link to that
  repository's History). Tiles above it give the totals of the whole board (active runs, running and blocked jobs, open pull requests, spend), whatever
  the filters. **Every repository is collapsed by default**: its row holds the name, the state, the same figures, how long ago its newest run was active
  and, while filtering, the number of matches (a repository with an error names the error there). Opened, it shows the false-incomplete rate, the jobs
  by status and the runs, newest activity first, each with its supervisor state, last activity, cost against `run_budget_usd` and its jobs (job with
  its issues and a *Links* fold, status, phase, elapsed time, cost, pull request, next action with blockers and **Stop**). **Expand all** and **Collapse
  all** act on the repositories shown. What the reader opened or closed is kept for the tab (in memory: across polls, filter changes and views, not a
  reload); while a search or status filter is set, a repository with a match opens unless the reader closed it under that search and status (another
  search or status forgets those, so it opens what it matches).
* **Filters of the Active board** (a toolbar above it). **Search** (plain text, case-insensitive, at most 100 characters) looks in the repository,
  run id, job id, status, phase, `#<issue>`, `PR #<n>` and next action; a matching run id or repository shows all its jobs. **Status** is a group
  (*Needs attention*: blocked, failed, pr_incomplete, needs_human_publish, stopped, interrupted; *In progress*: running, queued, not_started; *Ready*: pr_ready;
  *Done*: merged, closed) or one status word on the board, with its count. **Repository** picks one. **Sort**: most recent activity (the default) or
  name. **Show repositories with no active run** lists those with no run on the board (hidden otherwise, with a note saying how many; a repository with an error is
  listed whatever the search, status or idle setting). **Clear filters** clears the search, the status and the repository. The filters are kept in the address (`#/active?status=attention`,
  only values that differ from the default), so a reload or a shared link shows the same board; a change replaces the address
  (`history.replaceState`), so it adds no entry to the browser's history and loads nothing. The board follows each key at once; the address follows the
  search box once typing pauses (0.4 s), since browsers refuse a burst of address changes (a link or Back taken before that wins: the search is then
  not written).
* **Polling.** Every 15 seconds while the tab is visible (backing off up to 2 minutes on errors, paused while the tab is hidden) only the results are
  drawn again, never the toolbar, so typing is not interrupted; open repositories stay open and the focus on a repository's row comes back. A poll
  keeps the order on screen (a repository or run that recorded something newer does not move; a new one comes last): the order by activity is worked
  out again when the view is drawn whole or a filter or the sort changes (a repository the filters hid counts as new). The toolbar appears with the
  first repository a poll brings. A poll is not drawn while a stop confirmation or a stop in flight is in view (one inside a closed repository, or
  whose job the filters hide, holds nothing back), or while the focus is on a link, a fold or another element of the results that a redraw could not
  give back; the header then keeps the age of what is on screen, so it turns stale. A redraw (a poll, a filter change, **Expand all**, **Collapse
  all**, **Clear filters**) keeps an open stop confirmation, with its reason, and a stop in flight, whose answer is shown where it was asked. A poll
  that fails leaves the board, the toolbar and the focus as they are, with a notice above the board, and retries; only a first load, or a sign-in
  problem, puts the error in place of the board.
* **History filters**: Show (runs or jobs), Repository (suggesting the manager's repositories and those in the results), **Run** (jobs only, as in
  `/history/jobs`; disabled while showing runs), Outcome, From date, To date (inclusive) and Page size. The quick ranges **Today**, **Last 7 days**,
  **Last 30 days** and **Any time** set the first day as a UTC date (the ledger's dates are UTC) and clear the last. The filters in effect are chips,
  each with a × button that removes it.
* **States.** Loading, empty (no repository, no run, no match), and errors, each in one shared block (a title, then the text; an error is announced at once). The panel above the views says which sign-in state the tab is in:
  *not signed in* (a "Sign in with Microsoft" button, or, in token mode, the paste field with the reason), *signed in as <account>* (with "Sign out",
  which ends the Microsoft session too), *no Factory role* (a `403`: "Sign in with another account" and "Sign out"), *session expired* (a `401` that a fresh
  token did not fix: "Sign in again"), *sign-in failed* (the fixed-shape error code of Microsoft's answer, never its free text), *not configured* and *not
  available*. The views show `503` ("authentication is temporarily unavailable"), `404` and unreachable as before. A tab without a token shows an
  invitation to sign in, not an error. The account name is shown as text like everything else.
* **Accessibility and phones.** Landmarks (`header`, `nav`, `main`, `footer`), a skip link, labelled fields, captions and column scopes on tables,
  `meter` and `progress` instead of drawn bars, a visible focus ring, light and dark schemes built on design tokens (type scale, spacing, colours; dark follows `prefers-color-scheme`), a navigation that becomes a full-width tab bar on a phone, status chips (`running`, `pr_ready`, `pr_incomplete`, `blocked`, `failed`, `needs_human_publish`, `merged`) that always carry the status as text and the reason code where the manager gives one, reduced motion respected, and tables that scroll
  inside their own focusable region on a narrow screen; on a phone the job and history tables become stacked cards (each value under a small label, the
  column headers kept for assistive technology).

#### Design note and live check

A short summary of how the page looks, and the places for the screenshots an Operator adds after a live check. It describes
`src/factory/ui/style.css`; the stylesheet is the source of truth.

* **Look.** Cool slate neutrals with an indigo accent, an indigo-to-violet header with pill-shaped tabs, rounded cards with soft shadows, a coloured top edge on
  each total tile, and tabular figures. System fonts only.
* **Design tokens.** Sizes, spaces, radii, colours and shadows are custom properties on `:root`: the type scale (`--text-2xs` to `--text-3xl`), spacing,
  `--radius-*`, `--tap` (the minimum touch target), `--page-width`, the colours `--bg`, `--panel`, `--ink`, `--muted`, `--line`, `--accent`, `--focus`, `--subtle`
  and the header's `--header-*`, and an `--info`, `--ok`, `--warn`, `--bad`, `--neutral` and `--spend` family (colour, ink, tint and edge). Rules use the tokens;
  the few literal colours (the brand mark and the freshness pill on the header, the white text of the danger button) are the same in both schemes.
* **Light and dark.** Light is the default. Dark is one `@media (prefers-color-scheme: dark)` block that only redefines the colour and shadow tokens, so the page
  follows the browser's or the system's setting. There is no switch on the page and nothing is stored.
* **Status chips.** Tinted pills with a coloured dot: blue for running, queued, planning and draining; green for merged, pr_ready and ok; amber for pr_incomplete,
  needs_human_publish, stale, stopped and interrupted; rose for failed, blocked, abandoned, closed and error; slate for the rest. They always carry the status as text (and the
  reason code where the manager gives one), so colour is never the only signal.
* **Phone layout.** On a narrow screen (`max-width: 40em`) the navigation (Active, History) becomes a full-width tab bar with touch-sized targets, the toolbar fields
  stack (status and repository share a row), a repository's figures wrap below its name, and the rows of the job and history tables become stacked cards; other
  tables scroll inside their own focusable region.
* **Focus ring.** Links, buttons, fields, `summary`, the scroll regions and the log text show a 3px outline in the `--focus` colour on keyboard focus, in both schemes.

**Routes.** The page knows three hash routes (`parseRoute` in `app.js`): `#/active` (also the fallback for any other hash), `#/history?...` and
`#/job/<org>/<name>/<run>/<job>`. **There is no `#/run` route.** The Run view is one run's section in `#/active` (its jobs, cost against budget and next action) or,
for the run-level log files (`plan.log`, `supervisor.log`, `planner.*`), the run's files in the Logs panel, which is opened from a job view. The Logs panel exists only
on the job view of a live run.

**Screenshots.** Each placeholder below is replaced by an image after the live check, keeping its label. Switch the scheme with the browser's or the operating
system's colour-scheme setting, not with anything on the page.

| # | View | Scheme | Route or panel, and the state to capture |
|---|---|---|---|
| 1 | Active | light | `#/active`, signed in, a repository with a running run opened, status chips visible |
| 2 | Active | dark | `#/active`, the same state as 1 |
| 3 | History | light | `#/history?kind=runs`, a few rows, the filters visible |
| 4 | History | dark | `#/history?kind=runs`, the same state as 3 |
| 5 | Run | light | one run's section in `#/active` (jobs, cost, next action), or the run-level files (`plan.log`, `supervisor.log`) in the Logs panel of a job view |
| 6 | Run | dark | the same state as 5 |
| 7 | Job | light | `#/job/<org>/<name>/<run>/<job>`, phase timeline and attempts visible |
| 8 | Job | dark | `#/job/<org>/<name>/<run>/<job>`, the same state as 7 |
| 9 | Logs | light | the Logs panel of a live job, `#/job/<org>/<name>/<run>/<job>`, a file open, *Follow new lines* ticked |
| 10 | Logs | dark | the Logs panel of a live job, the same state as 9 |

<!-- screenshot 1: Active, light: #/active -->
<!-- screenshot 2: Active, dark: #/active -->
<!-- screenshot 3: History, light: #/history?kind=runs -->
<!-- screenshot 4: History, dark: #/history?kind=runs -->
<!-- screenshot 5: Run, light: a run's section in #/active, or the run-level log files in the Logs panel (no #/run route) -->
<!-- screenshot 6: Run, dark: a run's section in #/active, or the run-level log files in the Logs panel (no #/run route) -->
<!-- screenshot 7: Job, light: #/job/<org>/<name>/<run>/<job> -->
<!-- screenshot 8: Job, dark: #/job/<org>/<name>/<run>/<job> -->
<!-- screenshot 9: Logs, light: Logs panel of #/job/<org>/<name>/<run>/<job>, follow on -->
<!-- screenshot 10: Logs, dark: Logs panel of #/job/<org>/<name>/<run>/<job>, follow on -->

**Live check for an Operator** (a person with `Factory.Operator`, on the deployed manager; it needs that sign-in). Do it in light and again in dark, and tick each line:

- [ ] **Sign-in.** Open `https://<host>/ui/`. The panel says *Not signed in*; **Sign in with Microsoft** returns to `/ui/` with no code or token left in the address,
  and the panel says *Signed in as* your account.
- [ ] **Active** (`#/active`): only runs active in the last 12 hours or with a live supervisor appear, every repository is collapsed and opens to its runs, the header shows how old the
  data is, status chips carry their text, and **Stop** is enabled. A filter changes the address (`#/active?...`) and the rows, and survives a reload.
- [ ] **History** (`#/history`): the ledger lists runs; changing a filter changes the address and the rows.
- [ ] **Run**: a run's section in `#/active` shows its jobs, cost and next action (there is no `#/run` page).
- [ ] **Job** (`#/job/<org>/<name>/<run>/<job>`, from a link on Active or History): facts, phase timeline and attempts show.
- [ ] **Logs** (job view of a live run): the list shows the job's and the run's files; opening one shows the tail, and *Follow new lines* is **on by default**
  (the note says *Following*).
- [ ] **Follow mode.** On a log that is still growing, new lines arrive by themselves. Scroll the text up: the note says following is paused (scrolled up) and no new
  text is added. Scroll back to the bottom: following **resumes by itself**. Switch to another tab for a while and come back: following has paused while the tab was hidden
  (no polling) and resumes by itself, with the lines that arrived meanwhile.
- [ ] **Both schemes.** After switching the colour scheme, text, chips, tables and the focus ring (Tab through the page) are legible in both.
- [ ] **Phone width** (optional): the navigation is a tab bar and the job and history rows are stacked cards.
- [ ] Replace the placeholders above with the screenshots before merging.

### Browser sign-in

How an operator gets a token in the browser, end to end (the settings are above, in "The browser page"; the Azure side is `infra/README.md`, "Browser sign-in"):

1. The operator opens `https://<host>/ui/`. The page loads without a token (the shell is public) and reads `/ui/config.json`.
2. In `entra` mode the panel says "Not signed in". **Sign in with Microsoft** redirects the whole tab to `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`
   with `response_type=code`, PKCE (`S256`), the page's `redirect_uri` (`<origin>/ui/`), the scope from the config plus `openid profile offline_access`.
3. Microsoft returns to `/ui/#code=...`. MSAL redeems the code at the token endpoint (a CORS call to the one origin `connect-src` allows), and the page removes
   the response from the address. The access token is for this API's registration; the manager verifies its signature, issuer, audience, tenant, expiry and
   `roles` exactly as for a command line client (`entra_jwt.py`). `Factory.Viewer` reads, `Factory.Operator` reads and acts.
4. The token is renewed with the refresh token (a single-page-application refresh token lasts 24 hours); after that, or when the manager says `401`
   twice, one redirect signs the person in again (an Entra single sign-on session usually returns at once). Sign out clears this tab and ends the Microsoft session.

What the person sees is in `docs/operations.md`, "The dashboard in a browser".

### History ledger

`state.json` and the logs go with the run directory, and the janitor purges a run shortly after its jobs are merged or
closed, so on their own they say nothing about what Factory did last week. The **history ledger** (`src/factory/history.py`) is
what stays: an append-only record of runs and jobs in `${FACTORY_DATA}/history/`, **outside** `repos/<org>/<name>/`, so neither
`purge` nor `purge-clone` removes it, and a manager restart does not lose it. A worker's sandbox hides it like everything at
the top of the data directory except its own repository.

**What it holds: only ids, statuses, numbers, pull request URLs, reason codes, costs, token counts and durations.** Never agent-authored text
(a job's error, blockers, review findings, retry reasons), an issue's or pull request's text, a transcript, a prompt, or a
credential or secret of any kind (an API key, an access token, a signature). Token **counts** (the input, output, cache write and cache read
tokens of a session) are numbers, not secrets, and are recorded. Every field is checked against a fixed vocabulary or a strict pattern before it is written and dropped if it does not fit,
so the rule that agent text never leaves the run directory still holds. `reason_code` is one of `reason_codes.ORDER`; an
operator's `reason` is not stored at all, and the principal is the authenticated identity (the same value as in the audit line).

Entries are single JSON lines `{"v":1,"at":...,"repo":...,"run":...,"kind":...,...}`:

| `kind` | written by | fields |
|---|---|---|
| `planned` | the CLI `plan` | `issues`, `jobs` (`id`, `issues`), `planner_cost_usd`, `planner_tokens` (the token counts of the planner session of this `plan`, only when it reported any; the sessions of a later `plan --append-to` never reach the ledger, as with `planner_cost_usd`, so `status --json` can show more under `plan` than the ledger holds) |
| `run_started`, `run_finished` | the supervisor (`run`) | `result` of the end: `stopped` (finished), `drained` or `interrupted` (an exception) |
| `job` | every `State` of a managed run: the supervisor, `retry`, `block`, `resume`, `recover` and the other recovery commands | `job`, `issues`, `status`, `phase`, `attempt`, `pr`, `cost_usd` (cumulative), `tokens` (cumulative token counts by phase, only when a session reported any), `active_seconds`, `reason_code`; one entry per real transition (status, phase, attempt or pull request), not per update |
| `pr_observed` | the manager (cost guardrail and janitor) | `job`, `pr`, `handoff_status`, `observed_at`, `fresh` (per job: false when that job's live lookup failed, and the word is then only what `state.json` says, even if another job's lookup in the same read worked; a later fold never lets such a word replace one GitHub confirmed); only when the state changed. No GitHub call of its own |
| `purged` | the CLI `purge` (`reason` `manual`, from the persisted state) and the janitor (`terminal` or `abandoned`, with GitHub's word for each pull request) | `reason`, `jobs` (`id`, `status`, `handoff`, `pr`, `code`, and for the janitor's entry `fresh`: false when its status read could not look that job's pull request up), `fresh` (the janitor's entry only, for the whole read: true only when no lookup failed; a job's own `fresh` wins). Two entries for one purge fold into one |
| `operator` | the manager, for each successful `POST .../{action}` | `action`, `principal`, `job`, `http`. A command run by hand on the pod has no principal and no entry of this kind |

`tokens` is `{phase: {name: count}}`: only the phases of `phases.PHASES`, and in each only the names `input_tokens` (uncached input),
`output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and, when the CLI reported the split of the cache writes,
`ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`. A value is kept only if it is a non-negative whole number of at most 2^53 (a bool is
not one); a phase or name outside this vocabulary, and anything else, is dropped, so one entry holds at most 30 numbers, about 1 KiB more than before. The figures are
cumulative like `cost_usd`: a reader takes the latest one of a job. Entries written before these fields existed, and jobs without usage data, lack them, and a
reader treats that as no counts. How the counts are read and the hit rate defined: `docs/operations.md`, "Tokens and cache hit rate".

`POST /trigger-pause` is manager-wide, belongs to no run and is recorded in the audit line only.

**Opt-in by construction.** Nothing is written unless the repository is a managed one (`--repo`, so not local mode) and
`FACTORY_DATA` is set, and the run directory is the one under it; anything else is silently not recorded. The ledger is best
effort: a write that fails (a full disk, a symlink where the directory should be) costs history and never stops a run, a purge or
a request. The directory is `0700`, the files `0600`, opened without following symlinks.

**Bounded.** Segments `events-<YYYYMM>-<NNN>.jsonl`: a new one starts each month and when the current one reaches an eighth of
`FACTORY_HISTORY_MAX_BYTES`. When one starts, and once per janitor pass, segments whose last entry is older than
`FACTORY_HISTORY_RETENTION_DAYS` are removed, then the oldest while the total is over the cap. The total therefore stays at or below
`FACTORY_HISTORY_MAX_BYTES` (default 64 MiB) and nothing older than the retention (default 365 days) stays. An entry is
roughly 100 to 400 bytes and a run of a few jobs writes some tens of them, so the default holds the order of thousands of runs (an
estimate, not a measurement on a real deployment); raise the cap if the retention should win over the size. Writers
(the supervisor, CLI commands and the manager) append whole lines under an advisory lock (`history.lock` in the directory);
readers take no lock and skip a torn last line, an entry of a newer version and fields they do not know.

**Moving the data.** The ledger lives on the same Azure Disk as the run directories (`/data`), whose reclaim policy is `Retain`, so it
survives a pod replacement; it is lost with the disk. It is plain text: copy `${FACTORY_DATA}/history` to keep it elsewhere, or remove it
to start over (the next entry recreates it).

## Metrics listener

With `FACTORY_MANAGER_METRICS_PORT` set, the manager starts a second server (`metrics.py`) beside the API, in this process: it answers `GET /metrics` in
the Prometheus text format and nothing else (`404` for another path, `405` with `Allow: GET` for another method, `503` beyond 8 scrapes in flight). It
bypasses the identity check on purpose (the output is numbers and closed words, and a scraper has no Entra token), is kept off the audit sink, and is not
reachable on the API port, where `/metrics` is an unknown path (`401` without an identity, `404` with one). It is off by default and binds
`127.0.0.1` unless `FACTORY_MANAGER_METRICS_HOST` says otherwise; the startup line gains ` metrics_port=N` when it is on (and ` metrics_auth=bearer` with a token).
With `FACTORY_MANAGER_METRICS_TOKEN` set, every `GET` without `Authorization: Bearer <token>` is `401` (constant-time comparison; the token is never written anywhere), and
a host that is not loopback without a token is refused at start (exit 2, a fixed message). A scrape reads the manager's memory,
the history ledger (through the dashboard's bounded cache) and the retained runs' `state.json`; it starts no process and makes no GitHub call. The
listener stops first at shutdown, quickly, and its threads are daemons, so it never uses the shutdown budget. The catalogue, the label rules, the
threat model and what is not exposed yet are in `docs/metrics.md`.

## Leases (deviation from "acquire lease -> spawn")

The lease that keeps one supervisor per repository is taken by the **CLI process itself**
(`factory.locks.lock_from_env`, wrapped around every mutating command since Phase 2) and
held for the whole life of the supervisor. If the manager also held it, it would block its
own child. So the manager **probes**: before spawning it builds the same lock the CLI builds
(`lock_from_env(cfg.slug, <data>/repos/<org>/<name>/factory.lock)`: a blob lease on
`<org>__<name>` when `FACTORY_LOCK_ACCOUNT_URL` is set, otherwise a local flock), acquires
it and releases it at once. Busy is `409`, an unreachable backend `503`. The probe's
`on_lost` is a no-op so a lost probe lease can never terminate the manager. There is a
window between the probe and the child's own acquisition; if another supervisor wins it,
the child exits non-zero immediately and `GET /repos` shows its exit code. Fencing and
`assert_held()` stay entirely inside the CLI.

## Operations loop

Four jobs run in background threads (`src/factory/operations.py`; the fourth, the label watcher,
only when it is wanted, see below; the follow-up pass and the epic gate pass are steps of its trigger pass), started by `serve()`
after the listening line (never by `Manager.__init__`, so building a manager starts no
thread). `FACTORY_MANAGER_OPERATIONS=off` starts none of them. They are not new powers:
every command is a CLI argv the manager also runs for an HTTP endpoint, except `recover` (restart
recovery) and `purge-clone` (the janitor), which only the loop issues and which have no endpoint, and
`purge --unchanged-since`. The loop also writes the `auto-resumed` and `budget-drained` markers into run
directories itself. Commands go through the same injected runner in the same two lanes (`status`, `other`)
that bound the HTTP handlers, so background polling cannot starve an operator. A resumed or label-triggered `plan`, `run` and `retry` goes through the same code as
`POST .../plan`, `.../run` and `.../retry` (`Manager.start_plan`, `start_run`, `retry_job`: the spawner, the child registry, the lease probe, the one-process-per-repo
rule), so `pause`, `drain`, shutdown, the budget guardrail and `status` see it like any other child.

Every line is fixed-format with `escape_field` escaping and carries only ids, statuses and
numbers: never a job error, blocker, issue text, prompt, or any CLI stream. A pass that fails
is one stderr line naming the exception type (`factory-manager: operations <loop> failed
(<Type>)`) and the loop carries on with the next item and the next pass.

### Repository settings

Top-level keys of the repository's TOML file, all optional, re-read on every pass (so with a
locally edited `FACTORY_CONFIG` an edit applies without restarting the manager; on AKS the files come from
`repo_configs` through an immutable ConfigMap, so an edit is a new ConfigMap and a Deployment rollout (a pod restart): drain every
repository first, `infra/README.md`, "Rotate secrets and change repository configuration", and note that `auto_resume_after_restart` only matters at start):

| Key | Default | Meaning |
|---|---|---|
| `abandon_after` | `"7d"` (604800 s) | Idle time after which a run with no live worker is purged. `"<n>d"`, `"<n>h"`, `"<n>m"`, or an integer number of seconds of at least 3600. |
| `run_budget_usd` | none | Cost at which a run started by this manager is drained. A positive, finite number of dollars. No key, no guardrail. |
| `planner_max_usd_per_hour` | `2.0` | The most planner spend allowed per repository in any hour. Before every planner start of the label-trigger loop (a new `plan`, the plan of an epic phase, a top-up `plan --append-to`) the manager sums `total_cost_usd` of the repository's `planner-usage*.json` files in its run directories whose modification time is in the last hour; at or above the cap nothing is started and the loop logs `trigger repo=... skipped reason=planner_spend_cap` (rate-limited, counted in `factory_watcher_skips_total`), and planning resumes when the records leave the window. A finite number of dollars; `0` or `"off"` disables the cap; a negative, non-finite or non-numeric value is refused. A planner session that fails without writing a usage record is not counted. The cap does not apply to a plan a person starts through the HTTP action or the CLI. |
| `auto_resume_after_restart` | `"never"` | `"once"` lets the manager resume interrupted jobs after a restart (below). `scripts/register-repo.sh` writes `"once"` for a newly registered repository; the manager's own default for an entry without the key stays `"never"`. |
| `epic_max_open_prs` | `3` | The manager starts the next phase of an approved epic itself (section 6) only while fewer than this many pull requests of Factory's own App identity are open in the repository (1 to 50); a listing that is cut off, fails or has no App identity holds the start (`skipped reason=epic_cap`, `start-held` in the status comment). The per-run budget `run_budget_usd` applies to each phase's run and is never summed. |
| `epic_mode` | `true` | Epic mode: the planner may return `epic` for a too-large but decomposable issue and the epic gate pass (section 6) runs for the repository; `false` keeps `blocked` and makes no gate call. Needs `comment_on_blocked` and `auto_build` (the planner's `epic` becomes `blocked` without a comment channel or without the label-trigger pass that runs the gate). `FACTORY_EPIC_MODE=off` switches it off for the whole deployment. |
| `pr_style` | `"factory"` | Title and commit wording of a Factory pull request: `"factory"` is `factory: <objective> (#N)`; `"conventional"` is Conventional Commits `<type>: <subject> (#N)` with a squash-safe description, for a repository whose release tooling reads the merged commit message (see `docs/operations.md`, "Pull request conventions per repository"). |
| `execution` | `"inprocess"` | Where the repository's worker sessions run: `"inprocess"` is a child of the supervisor, as always; `"pod"` asks for a pod per session and is **refused** (the job fails with a fixed text, nothing is started, there is no fallback to in-process) while `FACTORY_POD_BACKEND` is not `on` and, with it `on`, whenever the pod backend cannot be built for the session; with it `on` and configured it is a pod per session (`PodBackend`, slice 12b, which nothing can run until the launcher and the relay are started). The supervisor reads it, not the manager's loops, and it covers every phase. Only the owner sets it; `docs/pod-backend.md`. |

The manager reads them through `operations.policy_of`, which gives the default for a setting
the config object does not carry and for any value outside these ranges (a malformed budget
is no guardrail, anything but `"once"` is `"never"`).

### 1. Restart recovery (once, after start)

For every configured repository and every run directory that has a `state.json`, in order:

1. Wait until the repository lease can be acquired and released, using the same probe as
   `plan`/`run` (`Manager.lease_state`), every 3 s for at most
   `FACTORY_MANAGER_LEASE_WAIT_SECONDS`. A supervisor that died with the previous container
   leaves a lease that expires on its own. On giving up there is one warning
   (`restart repo=<r> lease busy|unavailable after <n>s; recovery skipped`) and that repository
   is left for the next restart; other repositories go on. A backend that is unreachable counts
   as not free. The wait is a `threading.Event.wait` that shutdown sets.
2. Run the CLI `recover` (jobs whose worker is provably dead become `interrupted`; the CLI
   itself refuses while a child may be alive or a pid from another host cannot be proved dead),
   then `status --json`, and print one line:

   ```
   restart repo=<r> run=<r> interrupted=<n> queued=<n> pr_ready=<n> running=<n> pr_incomplete=<n> failed=<n> blocked=<n> needs_human_publish=<n> stopped=<n> merged=<n> closed=<n> not_started=<n> other=<n> recovered=<n>
   ```

   The status counts are jobs per raw status after recovery (`other` is any status not named);
   `recovered` is the number of jobs that became `interrupted` in this call. Failures print
   `restart repo=<r> run=<r> failed=recover rc=<n|timeout>` or `... failed=status recovered=<n>`;
   a process this manager started in the meantime prints `... skipped=process-running`. A run with a pod session that is still alive
   (or that the launcher cannot answer for) is **not** a failure and prints `restart repo=<r> run=<r> waiting=pod` once: it is asked again every
   30 s after the pass over the other runs, for at most the longest a pod can run (`DEADLINE_CLAMP_SECONDS` + `DEADLINE_MARGIN_SECONDS` +
   `TERMINATION_GRACE_SECONDS`, 14 730 s), then skipped with one warning (`restart repo=<r> run=<r> pod still live after <n>s; recovery skipped`). No
   job is marked `interrupted` meanwhile. Nothing of this happens for a run without a pod handle (`docs/pod-backend.md`, slices 19 and 20).

**It does not start `run`.** The default after a restart is that an operator looks at the
counts, then uses `retry`/`resume` and `run`. The one exception is opt-in.

### Automatic resume (`auto_resume_after_restart = "once"`)

After every run of the repository has been recovered, a run that has at least one `interrupted`
job and no file `<run>/auto-resumed` is resumed:

1. The marker `<run>/auto-resumed` is created first, exclusively (`O_EXCL`, mode `0600`, content
   a timestamp only). **Once means once per run directory, ever**: a crash anywhere afterwards,
   a crash loop, or another restart can never resume the run a second time; purging the run
   removes the marker with it.
2. The CLI `retry <job> --reason=automatic resume after manager restart` for each interrupted
   job (job ids from the status document are checked against the job id pattern before they
   reach argv).
3. If at least one retry succeeded, `run` is started as a detached child through the same code
   path as `POST .../run`, with the CLI's defaults, and registered in the child registry.

A run the manager started for an epic phase (its directory holds the `epic-phase` stamp) is first proven again from GitHub, before the marker is claimed and before any retry
(`operations._epic_verdict`, the same verdict as the build of a planned phase; `docs/operations.md`, "Automatic start"). When the approval no longer stands, or cannot be proven (a failing or
cut-off read, a stamp that is not valid), the run is left interrupted: nothing is retried or started and the marker stays free, so a later restart or a person's `resume` can go on.
At most one run per repository is started (the registry refuses a second; that run prints
`resume repo=<r> run=<r> skipped=process-running`). Lines:

```
resume repo=<r> run=<r> retried=<n> action=run pid=<pid>
resume repo=<r> run=<r> retried=<n> action=run-refused status=<http status>   # lease held, no plan, ...
resume repo=<r> run=<r> job=<id> retry=failed rc=<n|timeout>
resume repo=<r> run=<r> retried=0 action=none
resume repo=<r> run=<r> skipped=already-resumed | process-running
epic repo=<r> issue=<P> phase=<k> child=<c> resume refused: the approval no longer stands | the approval could not be proven   # an epic phase run
trigger repo=<r> run=<r> resume refused reason=stamp   # an epic stamp that is not valid
```

### Trigger pause (a restart switch)

A marker file `trigger-paused` at the top of the data directory (`$FACTORY_DATA/trigger-paused`, mode `0600`, content a
timestamp) is the pause. It is created and removed only by `POST /trigger-pause`, and it is a file so that it **survives a
manager restart**: a deployment that uses the deploy gate (`tools/infra_deploy_gate.py`) sets it, the pod restarts, and the new manager still starts nothing until the apply's last
step clears it. While it exists the manager starts **no new plan and no new run by itself**:

* the label trigger (section 4) reads no issue and starts no plan, and does not finish a plan that is waiting for its `run`
  (one line `trigger skipped reason=paused` the first time, then silence; no GitHub call is made);
* the dependency resume (section 4) starts no `run` for a run whose dependency merged;
* the restart auto-resume (`auto_resume_after_restart = "once"`) is **held back, not dropped**: it logs
  `resume repo=<r> run=<r> skipped=trigger-paused` without claiming `auto-resumed`, keeps the run in memory, and the recovery thread
  (which waits for this, so a restart during a deploy loses nothing) runs it once, when the pause is cleared; `auto-resumed` is claimed
  only then, so the run is resumed at most once. Recovery itself (`recover`, the counts line) still runs. The list is not persisted: a
  manager that restarts again while paused recovers again and holds the resume back again. A run **drained** by the deploy gate is
  not `interrupted`; the gate's last step starts `run` for it after clearing the pause (`infra/README.md`, "Safe deployments");
* the in-process starts (`Manager.start_plan`, `Manager.start_run`) refuse with `503 trigger paused`, so a pass that began
  just before the pause cannot slip a start in (the callers treat `503` as "busy" and look again later).

The stop-the-world breaker (`docs/capacity.md`) pauses the trigger through a second marker, `spend-breaker`, with the same effects:
`trigger_paused()` is true while either exists, `POST /trigger-pause` sets and clears only the manual one (its reply reports that marker,
so the deploy gate's check still passes), and the breaker is cleared only by `POST /spend-breaker`.

Not affected: a run in progress (nothing is drained, paused or signalled; a tripped breaker does drain), the cost guardrail, auto-purge, and everything an
Operator asks through the HTTP actions (`plan`, `run`, `retry`, ... still work while paused: it is a switch for the
automation, not a lock). A pause that is left set blocks every label-triggered build; `GET /repos` shows
`"trigger_paused": true` so it cannot be forgotten silently.

### 2. Cost guardrail (every `FACTORY_MANAGER_POLL_SECONDS`)

For each `run` child this manager started whose repository sets `run_budget_usd`: `status --json`
in the `status` lane; if `cost_usd` (the whole run including the planner) is at or above the limit
and this process was not drained for budget yet, the CLI `drain` (the same argv as
`POST .../drain`) and one line:

```
budget repo=<r> run=<r> cost=<x.xx> limit=<y.yy> action=drain
budget repo=<r> run=<r> cost=<x.xx> limit=<y.yy> action=drain-failed rc=<n|timeout>
```

The status read each pass makes also feeds the history ledger: a job whose pull request state changed since the last pass gets a
`pr_observed` entry (no extra GitHub call; "History ledger").

A drain that succeeded is remembered in memory and in `<run>/budget-drained` (written atomically,
content a timestamp only), so one run is drained once, not every minute, and a restarted manager does
not drain it again. A failed drain is not remembered and is tried again on the next pass. The marker
covers the process that was running when it was written: a run that an operator starts again later
(a new process, newer than the marker) is guarded afresh, so raise the limit in the config first
if the run should continue. Only runs this manager started are watched; a supervisor started by
hand with the CLI is not, and after a manager restart only runs started since then are.

`drain` stops new work; the phases in flight finish and are still paid for, and `cost_usd` is
the supervisor's own estimate, so the real spend ends above the limit. The Console workspace
spend limit is the hard cap; this is the soft one.

### 3. Auto-purge (every `FACTORY_MANAGER_JANITOR_SECONDS`)

For each configured repository and each run directory, skipping every repository that has a
process this manager started and every run whose lease probe is not free:

* **terminal**: at least one job, and every `jobs[]` entry of `status --json` has `status` in
  `{merged, closed}` or `handoff_status` in `{merged, closed}`;
* **abandoned** (not rescued by pull requests or by a job waiting in `pr_ready`): no live worker (`active_worker_count == 0`, and `supervisor` null or not
  `running`/`draining`) and the newest modification time of `state.json`, `plan.json` and
  `supervisor-control.json` is older than the repository's `abandon_after`. A run that never
  started has no status and is judged by its files alone; a run directory with none of those
  files (a plan that failed early) by the directory's own time. A status document that
  cannot be read, or lacks the worker count, protects the run, and so does a job in
  `needs_human_publish` (finished work whose commit is only in the run, waiting for a person to
  push it): such a run is never called abandoned, and the manager warns once per process
  `operations: purge kept repo=<r> run=<r> reason=awaiting_person`.

Then the CLI `purge --unchanged-since=<ns>` for the run, where `<ns>` is the newest modification time of those
files as the janitor saw it before reading the status: the CLI compares it under the lease and refuses (exit 1)
if an operator command used the run in between. The lease is probed once per repository and pass (a busy one
skips the repository silently, an unavailable backend warns once), and the first pass waits until restart recovery
is over, so the probes cannot make a `recover` fail. Exit 0: the manager forgets the run's recorded exit, adds a `purged` entry to the history ledger (why, and GitHub's word for each pull
request: the run is gone, its history is not) and prints
`purge repo=<r> run=<r> reason=terminal|abandoned`. The CLI has by then also released the `factory:*` status
labels of the run's issues (a merged pull request leaves `factory:merged`, an open one keeps its label, an issue
that another run's plan lists is left alone; best effort, see `docs/operations.md`, "Purge"), so an issue of an
abandoned run can be planned again by a new `factory-build`. Non-zero or a timeout (the CLI refuses a run
that is not safely dead): `purge-skipped repo=<r> run=<r> reason=<..> rc=<n|timeout> cause=<code>`, never an
error, and no label is changed (`cause` is a fixed code from the first `factory: ...` stderr line of the CLI, never its text: `unchanged_since`, `alive`, `pod_live`, `foreign_pid`, `branch_checked_out`, `other_repo`, `state_unreadable`, `lease_busy`, `git_failed`, `remove_failed` or `other`; `docs/operations.md`). When a repository's `runs/` directory is empty and its clone is on disk (after this sweep purged the last run,
or from an earlier sweep whose removal failed or was killed), the CLI `purge-clone` runs (it re-checks under the lease,
and the clone is re-created on first use):
`purge-clone repo=<r> removed=yes|no` or `purge-clone-skipped repo=<r> rc=<n|timeout>`.

**Idle reconcile.** For every run the sweep keeps (and, in the label-trigger loop, for every repository where no process of
this manager is working), a run whose `state.json` holds a job with a published pull request (`pr_ready` or
`pr_incomplete` with a `pr` URL), or a job that ended (`blocked`, `failed`, `needs_human_publish`, ...) holding an observation
that no issue is known to carry yet (a flush GitHub refused; `observed.json`, `docs/operations.md`, "Issues opened by Factory"),
gets the CLI `reconcile` (the janitor only after its lease probe says free; the CLI takes the
lease itself and ends quietly with exit 0 when it is busy), at most once per five minutes per run. It sets `factory:merged`
on the issues of a job whose pull request was merged and clears the `factory:*` label of one closed without merging
(never an issue of a sibling run's plan or one labelled `factory-ignore`), and answers the completion label
`factory-verified` on an incomplete pull request (permission check, head check, `Refs` to `Closes`, gate re-post):
`docs/handoff.md`, "After the supervisor is done". It writes only labels, one pull request body and commit statuses (the completion gate, and the resolution of a pending `factory/gate`, `docs/handoff.md`, "The pending gate"; the latter only while no supervisor works in the repository), all
best effort: a failure is swallowed in the CLI, and a refusal is one rate-limited line
`reconcile-skipped repo=<r> run=<r> rc=<n|timeout> cause=<code>` (the same fixed codes). `reconcile` never changes a job's status in `state.json`: a merged job keeps reading `pr_ready` until its run is purged, and the janitor's terminal verdict uses the live `handoff_status`. A run with neither a published pull request nor such an observation (or with
issues for observations switched off) is never asked.

**History bounds.** After the repositories, the sweep applies the history ledger's retention and size cap (`Ledger.prune`).

**Log archive.** Each purge copies the run's logs first ("Log retention"); the sweep then removes retained copies older than `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS`, at most once a day.

**False-incomplete refresh.** After each repository's purge and reconcile steps, whether or not it has runs, the sweep
refreshes that repository's `false_incomplete` value (`GET /repos`, above; `docs/operations.md`, "False-incomplete
rate"): read-only GitHub calls in the janitor's own thread, at most once an hour per repository, with no lease, no CLI
command and no write to GitHub or the data directory. It needs `FACTORY_GITHUB_APP_SLUG`. A failure is one fixed line
`false-incomplete repo=<r> refresh failed error=<type>` (rate-limited) and the old value stays, marked `stale`.

### 4. Label-triggered builds (every `FACTORY_MANAGER_TRIGGER_SECONDS`)

**Nobody calls `plan` or `run` by hand.** The manager watches **every registered repository** (every
`repos/<org>__<name>.toml`). The per-issue label is the safety switch: nothing starts unless an authorised person
labels an issue `factory-build`. The decisions are in `src/factory/trigger.py` (pure, unit-tested without
threads), the loop is `Operations.trigger_pass` next to the guardrail and the janitor, and the HTTP API
(`plan`, `run`, `retry`) stays as the break-glass path.

**The loop exists** whenever `FACTORY_MANAGER_TRIGGER_SECONDS` is above 0, even when no repository is registered yet: every pass
re-reads the repository files and skips an opted-out one without a GitHub call, so registering a repository, or switching
`auto_build` to `true`, takes effect at the next pass with no restart. `auto_build = false` opts one repository out
(one line `trigger repo=... skipped reason=disabled`, once); `FACTORY_MANAGER_TRIGGER_SECONDS=0` or
`FACTORY_MANAGER_OPERATIONS=off` turns it off everywhere. The first pass waits for restart recovery to end, like the janitor.

Settings (top-level keys of the repository file, re-read on every pass):

| Key | Default | Meaning |
|---|---|---|
| `auto_build` | `true` | `false` opts the repository out of the watcher. |
| `auto_build_label` | `"factory-build"` | The label that asks for a build: 1 to 50 characters of letters, digits, space, `.`, `_`, `/`, `-`; never `factory-ignore` or a `factory:*` label. |
| `auto_build_min_permission` | `"write"` | The repository role the user who applied the label must hold: `read` < `triage` < `write` < `maintain` < `admin`. Lowering it below `write` is **refused for a repository that is not private**, checked at every poll (a repository made public later is refused from then on: `skipped reason=config`). |
| `auto_build_concurrency` | `1` | `--concurrency` of the automatic `run` (1 to 64). `run_budget_usd` applies as to any run. |
| `auto_build_max_issues` | `3` | At most this many issues per cycle (1 to 50), the lowest numbers first; the rest keep their label and wait for the next cycle. |
| `auto_build_timeout_seconds` | `1800` | The per-session limit of an automatic run, in seconds (60 to 86400): the `timeout` that the loop passes to `Manager.start_run` (new runs, retries and dependency resumes, and the run an automatic resume after a restart starts) and to the planner (`start_plan`, and `start_append` for a top-up). **A value outside the range, or not an integer, is not an error: the repository loads and uses 1800**. Read on every pass, but a run that is already running keeps the limit it started with. |

**The session limit.** Every agent session (the planner, and each build, review and verification session) is killed when it has run for `timeout` seconds. A hand-started
`POST .../run` takes `timeout` (1 to 86400) in its body; a run the loop starts takes it from `auto_build_timeout_seconds`. The planner is told the limit in its prompt
and is asked to give grouped issues that would need more time separate jobs (one issue is always one job), and to say so in the disposition reason. A session that hits the limit leaves the job `failed` with
the fixed reason `timed out after N s` (not the command line): `status --json` shows `failure: "timeout"` and `timeout_seconds: N` for the job, `next_action` says to raise
the key or split the issue, and the outcome comment on the issue says the same. **Raise the key** when the issue is one coherent change that a session simply needs longer for
(a large but cohesive refactor, a slow test suite); **split the issue** when it is really several changes (the builder was still adding unrelated parts when it was killed), because
a longer limit only lets a too-large job run longer and costs more per attempt (`run_budget_usd` and the per-phase `--max-budget-usd` caps still apply). Raising it on AKS is a rollout
like any config change (`docs/operations.md`, "Where the settings live").

**Which issues.** Open issues (never pull requests) that carry the trigger label, found with one `GET repos/{r}/issues?labels=...`.
An issue is eligible when it does **not** carry `factory-ignore` (ignore always wins) and carries **no `factory:*` status label** (it is already
in flight or done). Archived repositories are skipped. One exception, the retry request below.

**Who may trigger it.** Applying a label needs only *triage* access, which is less than the *write* access the `@mention` feature
requires, so the label alone proves nothing. The loop reads who applied it, from `GET repos/{r}/issues/{n}/events` (the **newest `labeled`
event for that label name**, paged 100 at a time; past 3000 events it gives up and treats the label as unauthorised), and requires that
actor to be a `User` (never a bot or an organisation) whose role from `GET repos/{r}/collaborators/{login}/permission` is at least
`auto_build_min_permission`. Anyone else is ignored and the label is **left in place**, with one line `trigger repo=... issue=N skipped reason=unauthorised`
(no login is logged). The refusal is remembered in memory until the issue is updated again (any label change updates it), so an unauthorised
label costs two GitHub calls, not two per cycle; a restart just repeats them.
Issues are looked at lowest number first and only while fewer than `auto_build_max_issues` of a kind (new, retry) have been accepted are further ones authorised, so an idle
cycle costs a bounded number of GitHub calls however many issues carry the label. **The label approves the issue as it reads when the plan snapshot is taken**: the label
authorises the issue number, not its text, and anyone who may edit the issue or comment on it before the plan runs (at most one interval, plus any wait for the lease) changes what is
planned. On a public repository that is a reason to keep `auto_build_min_permission` at `write` or above and to review the issue before labelling it.

**Model and effort.** Besides `factory-build`, an issue may carry at most one `factory-model:<alias or id>` and at most one
`factory-effort:<level>` label (`src/factory/model_choice.py`). They choose the Claude model and effort of the **build and verify** phases
of the job that owns the issue; the reviewer stays on the repository's own configuration and the planner is not changed per issue. Precedence,
most specific first: the issue's labels, the repository's `[models]` / `[efforts]` per phase, the repository's `model` / `effort`
defaults (plan, build, review and verify, never the preflight probe), then the built-in table. These repository file keys are read by the CLI
(`plan`, `run`) and re-read at every start; the `[models]`-only configurations that exist today behave exactly as before.

| Key | Default | Meaning |
|---|---|---|
| `model` | built-in table | Default model id of every phase but the preflight probe that has no `[models]` entry (`claude-sonnet-5-5`, `vendor.model-x`, ...; 1 to 128 characters of letters, digits and `. _ : @ [ ] -`). |
| `effort` | built-in table | Default effort of those phases: `low`, `medium`, `high`, `xhigh` or `max`. |
| `[efforts]` | empty | Per-phase effort (`plan`, `build`, `review`, `verify`, `preflight`), like `[models]`. |
| `allowed_models` | the built-in ids plus the ids this file configures (`model`, the `[models]` values, the `[aliases]` targets) | The model ids an issue's label may select. **An explicit list is exact**: nothing else is added, not even the built-in ids or the repository's own `model`. An empty list forbids every model label. |
| `max_effort` | the highest effort the built-in table and this file's own `effort` / `[efforts]` use (`high` unless the repository raises it) | The highest effort an issue's label may select. An explicit value is exact in both directions. A default `effort` above it is allowed: the cap only bounds what an issue may ask for. |
| `[aliases]` | `opus`, `sonnet`, `haiku` = `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` | `alias = model id`. Aliases are lowercase (`a-z`, digits, `.`, `_`, `-`, at most 36 characters, so the label fits GitHub's 50), one-to-one and matched case-insensitively with no prefix or fuzzy matching; an entry redefines a built-in alias or adds one. At most 32. |

**The labels are validated, never trusted.** When an issue is planned (a new plan, and an append to a running run, from the labels read in
that same snapshot) its labels are checked: an alias or a full id that `allowed_models` lists for the model, a level of
`low`/`medium`/`high`/`xhigh`/`max` that is not above `max_effort` for the effort. The label text is compared, never executed or logged.
An invalid or forbidden choice does **not** silently fall back to another model: the issue gets a **`blocked` disposition** with a fixed reason
(no job, no worker, and no planner tokens when no issue is left to plan), which the outcome comment (#32, #84) then posts without quoting the label.
Re-applying `factory-build` after fixing the labels plans the issue afresh (no job exists, so nothing stale is retried). The reasons, as fixed
codes: a choice label whose newest applier may not start a build here, or cannot be established (`label-not-authorised`, see "Who may set it"), a second `factory-model:` label (`duplicate-model-label`), a second `factory-effort:` label (`duplicate-effort-label`), a value that is neither an alias nor a model id
(`unknown-model`), a model that is not an alias of the repository and not in `allowed_models` (`model-not-allowed`; this also covers an unknown alias), an effort
that is not a level (`unknown-effort`), an effort above `max_effort` (`effort-above-max`). Log lines carry only the issue number and those codes:
`factory: issue directive refused issue=N reason=<code>[,<code>]`. Factory's own status labels use another namespace (`factory:`, a colon, never `factory-`) and are never read as a choice; neither is anything in the issue body, title or comments.

**One job, one choice.** A job may group several issues. The planner is told the choice of each issue and to keep issues with different choices in separate jobs
(an issue with no choice differs from one that has it); if its job still mixes them, the issues of that job, and of every job that depends on it, become `blocked`
dispositions (`conflicting-directives`, `dependency-blocked`) instead of one choice silently winning. Whatever `choice` the planner writes is discarded: Factory records it.

**What is recorded.** The resolved model and effort of build and verify, with the source of each (`built-in`, `repository`, `repository-phase`, `issue`), are written once
into every job of `plan.json` (and of an appended plan fragment) as the optional validated field `choice`; an alias is resolved at planning time, so changing an alias target
later never changes a run in flight, and a resumed, retried or follow-up session of the job uses the recorded values. A job planned before the field existed has none and is resolved from the live
configuration as before. `status` shows it (`Models: build <id> (<source>), effort <level> (<source>); verify ...` and `models` in `status --json`) and the pull request has a `## Models`
section with the same facts (and the reviewer's, from the configuration). The per-phase budgets and `run_budget_usd` apply unchanged: `--max-budget-usd` does not depend on the model.
The label set (`factory-model:<alias>` for each alias in force and `factory-effort:<level>` for each level) is created with fixed colours by `plan`, best effort, the same way as the `factory:*`
labels (one line `factory: model label sync failed (<exception class>)` if GitHub refuses, and planning goes on).

**Who may set it.** Applying a label needs only *triage* access, which is less than the bar for `factory-build` (`auto_build_min_permission`, `write` by default), so the label alone proves
nothing. When an issue that carries a choice label is planned (a new plan, and an append), Factory reads who applied each of its `factory-model:` and `factory-effort:` labels (the **newest `labeled`
event** for that label name, from `GET repos/{r}/issues/{n}/events`, one read per issue, `trigger.LabelAuthority`) and holds that person to the same bar as the trigger: a user, never a bot, whose
role on the repository (`GET repos/{r}/collaborators/{login}/permission`, read once per person per plan) is at least `auto_build_min_permission`, on a repository where that minimum is allowed at all (below `write`
only on a private one). Every choice label of the issue must pass. Anything else fails closed and **blocks the issue** with `label-not-authorised`, before the values are looked at: no `labeled` event found,
more events than the 3000 that are read, an unknown or custom role, a bot, a person who has left the repository. The label is never replaced by a default and nothing of it is quoted; a person with write access
removes it, or applies it again (the newest applier counts), and re-applies `factory-build`. A read that fails (a rate limit, a network error) does not block anything: `plan` stops with the fixed line
`factory: could not establish who applied the model or effort label`, plans nothing and writes no `plan.json`, so a later plan reads again. An issue without a choice label costs no extra GitHub call. The operator's
`allowed_models` and `max_effort` still bound what even an authorised person may ask for. There is no body syntax in this version.

**When it starts work.** Per repository and cycle, at most one start. While **a supervisor (`run` child) of this manager is active** for the repository, the loop tops that run
up instead of waiting (next section). With any other process of the repository running (a plan, an append, or a supervisor whose run has no `state.json` yet) it waits: `skipped reason=busy`. Otherwise,
when this manager runs no process for the repository, the repository lease must be free (else `skipped reason=busy`, once until something changes; the loop never waits on a lease), and the steps are, in this order:
"Busy" is decided by the child registry and the lease, not by a run's `state.json`: a supervisor that died leaves `running` in its state until restart recovery, and a free lease
proves that nobody works on that run, so refusing to start new work for it would stall the repository until the next restart. The run is not touched; `status` shows it.

1. a run that is **owed** or **pending**. *Owed*: a retry queued jobs of a run and its `run` did not start (busy lease, shutdown, a restart in between); the run directory holds a
   `trigger-run-owed` marker (the number of issues retried), so the run is started after any restart. *Pending*: a run this loop planned (an `auto-*` directory holding the loop's
   `trigger-run` marker, which it writes before it starts the plan; a run a human planned by hand is never finished by the loop, whatever its name) that has a `plan.json` and no
   `state.json`. Its issues lose the trigger label (until it worked), then `run`. Both steps check the repository first (archived, or a permission minimum below `write` on a
   repository that is not private) like a new plan. This is what makes the loop restart safe: it derives everything from labels and run directories, and holds in memory only
   pacing (backoff, rate-limited lines), the plan it started and the issues whose label would not come off (below).
2. a **dependency-merge resume** (below), for a started run whose queued jobs wait for the merge of another job's pull request;
3. a **retry** request (below);
4. **new work**: `plan --issues=N,...` for the eligible issues (`Manager.start_plan`, the same function as `POST .../plan`), run name `auto-<UTC timestamp>`
   (`auto-20270115T080000Z`, `-2` on a collision). When that plan child has exited and written `plan.json`, the next cycle takes step 1.

**Topping up a running run.** A busy repository is no longer always a wait. When a supervisor child is active, the loop decides from the **run directory** (`plan.json`, `state.json`, `inbox/`, `supervisor-control.json`
and the planner usage files, through the refusals of `append.check_run`) and from GitHub labels, never from memory, and starts at most one append per repository and pass:

* **free slots**: `auto_build_concurrency` minus the jobs in `state.json` that are `running` in a build phase (verification and review workers do not count). With none free: `skipped reason=slots_full`.
* **other refusals** (the same ones `plan --append-to` makes): `skipped reason=run_full` (jobs plus waiting fragments are at `fragments.MAX_RUN_JOBS`), `skipped reason=budget` (the run's cost, jobs plus planner usage, has reached
  `run_budget_usd`) and `skipped reason=draining` (the supervisor has a drain request). A run without a readable plan or state is `skipped reason=busy`. These lines are rate-limited like every `skipped` line.
* **planner spend**: an append, like every plan, is not started while the last hour's planner spend has reached `planner_max_usd_per_hour`: `skipped reason=planner_spend_cap` (see "Repository settings").
* **which issues**: the eligible, authorised issues as for a new plan, lowest number first, at most `min(free slots, auto_build_max_issues)`. Retry requests wait until the repository is idle. An issue already in `plan.json` or in a fragment
  waiting in the inbox is **never planned again**: its trigger label is only taken off.
* **the start**: `plan --issues=N,... --append-to <run>` through `Manager.start_append` (no lease probe, own registry slot and `append.log`; see "plan and run"). Line: `trigger repo=... issues=N run=... appending`.
* **the label** is taken off only once a fragment for the issue exists in `<run>/inbox` (or the supervisor already ingested it into `plan.json`), checked from the run directory on the next pass. A restart between the fragment write and the label removal therefore
  only removes the label. From then on the issue follows the normal `factory:*` status labels.
* **failure**: an append that exits non-zero, or exits without having written a fragment for any of its issues, keeps the labels, logs `trigger repo=... run=... append failed rc=N backoff=60s` and starts the per-repository
  exponential backoff exactly like a failed plan (a refused start is `append start refused status=N`). While an append runs, no second one can start for the repository (`skipped reason=busy`).

**The label is consumed after the plan succeeded**, never before: the plan child produced a `plan.json` (its exit code is not looked at; a plan without the file is a failure) and the
file lists the issue. Then `DELETE .../issues/{n}/labels/{label}` (a failure is one line `trigger repo=... label-remove failed error=<Type>` and the build goes on). Normally a leftover
label is harmless because the issue carries `factory:planned` and counts as in flight (an issue that only got `factory:blocked`, such as one the planner investigated, is a retry request
instead, so for it the memory below is the only guard), but the status labels are best effort too (same credentials), so the loop does not rely on
them: an issue whose label could not be removed is remembered in memory together with the id of the `labeled` event that was built, and is **not started again** (`skipped reason=unremoved`, once)
unless the label is applied anew (a new event, which goes through the authorisation like any request) or the label is gone; every cycle tries the removal again. A restart forgets this memory, which
costs at most one more build of such an issue. To rebuild an issue, add the label again once its status labels are gone. The whole life of the issue label: `factory-build` -> (plan succeeded, label consumed) -> `factory:planned` -> `factory:building` ->
`factory:in-review` -> `factory:pr-open` -> `factory:merged`, or `factory:blocked` / `factory:ci-failed`.

**Retry.** Applying the label to an issue whose status labels are only `factory:blocked`, `factory:ci-failed`, `factory:needs-info`, `factory:awaiting-approval`, `factory:plan-approved`, `factory:observed` and/or `factory:epic-phase` asks for another attempt. (`factory:epic-phase` is the origin label of a phase issue Factory opened for an approved epic, section 6: the manager starts the phase itself once the epic is approved; a person may still apply the trigger label to build it by hand, and Factory never applies it. The two epic labels: an epic plan, section 6. `factory:observed` marks an issue Factory opened itself for a worker's observation, `docs/operations.md`, "Issues opened by Factory": no run holds it, so applying the label plans it afresh; it is an origin label and stays on the issue after the job's own status labels are set.) The loop looks at the started runs, newest
first, and the first run whose plan has a job for the issue, or an `investigation`, `blocked` or `epic` disposition for it, decides (a `covered` disposition is passed over: it only says another job's
work resolves the issue). When that is a job, the loop runs `retry <job> --reason=label-triggered retry` (`Manager.retry_job`, the function behind `POST .../retry`, in the `other` lane), consumes the
label and starts `run` on that run. When it is an investigation or a blocked disposition, the newest plan chose not to build the issue, so an older run's job is not retried in its place (that
plan never saw the answers given since) and the issue is planned afresh; the same happens with no such run at all (it was purged). An investigated or blocked issue carries `factory:blocked`, so
applying the label again once its questions are answered is how it is planned again, and an issue that carries only `factory:blocked` is always a retry or re-plan request. A job that is `blocked` with the reason `dependency X was closed without merging` (the dependency-merge resume below wrote it) is treated as having no owner too: a retry would restart it under its old plan, which still holds the dependency, so the loop logs `issue=N retry replanned reason=dependency-closed` and plans the issue afresh in a new run against the current `main` instead (`Operations._owner`). A retry the CLI refuses (for example the job is already merged or running, or its CI
failure cannot be diagnosed) keeps the label, logs `retry refused ... reason=cli-refused` and pauses **that issue only** (60 s, 120 s ... up to an hour); the other issues of the repository are still planned in the same pass. Any other status label
(`planned`, `building`, `in-review`, `pr-open`, `merged`) means in flight or done and the trigger is ignored (`skipped reason=in-flight`).

**Dependency-merge resume.** The planner serialises jobs that touch the same code: job B has `depends_on: [A]`, and a `run` leaves B `queued`
("dependency merge: A") and exits once nothing else can move. Nothing inside the run notices the merge afterwards, so the loop does. On every pass, for a repository with no
process of ours and a free lease, it looks at each **started** run (a `state.json`) for `queued` jobs whose `depends_on` still holds a job that the persisted state does not call `merged`.
It decides from the run directory (`plan.json`, `state.json`) and from GitHub only, never from memory, so a manager restart between two passes changes nothing. For each such dependency it reads the
state of the pull request in its `pr` URL (`GET repos/{r}/pulls/{n}`, projected with `--jq` to `state` and `merged`; at most 20 pull requests per pass, the rest wait for the next one):

* **every unmerged dependency of the job is merged** (a job with two dependencies waits for both): the run is ready, and the loop starts `run` for it, once, through `Manager.start_run`, the function behind `POST .../run` (same
  one-process-per-repository registry, same lease probe, `--concurrency` from `auto_build_concurrency`). The repository checks of step 1 (archived, permission minimum) apply, and a run that has already spent its `run_budget_usd` is not started
  (`skipped reason=over-budget`), as in the automatic resume after a restart. `run` refreshes the state of the merged dependencies from GitHub when it begins, so afterwards the job is no longer waiting for a merge. A process or a lease that is busy
  gives `skipped reason=busy` and nothing starts; the next pass looks again, so a resume is never a duplicate start.
* **a dependency's pull request was closed without merging** (or its job is already `closed`): the dependent job can never start. The loop runs `block <job> --reason=...` (a CLI command that, under the repository lease, turns a *queued* job into
  `blocked` with the reason `dependency <job id> was closed without merging`) and the CLI sets `factory:blocked` on the job's issues, best effort like every status label. A blocked job is not queued, so no later pass looks at it; a refused `block`
  (for example a busy lease) is retried only after 60 s, 120 s ... up to an hour. A person can still retry it (the trigger label on the `factory:blocked` issue, or `retry`) once the cause is dealt with.
* **otherwise** (the pull request is open, or the dependency has none yet: it is still building, failed or blocked) nothing starts, and one line `skipped reason=dependency` per run is logged, at most once an hour.

The marker `dependency-resumed` in the run directory lists the jobs that this resume has answered, one line `<job> <starts> <epoch seconds>` each. It keeps the loop from starting the same run over and over, and from losing a job:

* a `run` that **exits non-zero** (the lease taken between the manager's probe and the CLI's own lock, a failed fetch, GitHub unreachable inside `run`) takes its jobs back out of the marker and logs `run failed rc=N backoff=...`; the repository backoff (60 s, 120 s ... an hour) then paces the next decision, which is taken afresh from GitHub;
* a `run` that **exits cleanly** with the job still queued (for instance it was drained) uses up the job's starts: the loop does not start it again, and a person does. The line `job=... skipped reason=resumed` says so, at most once an hour;
* the exit code lives in the manager's memory only, so after a **manager restart** the marker alone decides: a job still queued is started again after 10 min, then 20 min, and at most 3 starts are recorded in all, after which it needs a person (`skipped reason=resumed`).

**Shared-path waits.** The scheduler also keeps jobs whose paths overlap (`likely_paths` plus `actual_paths`; `.` or no known path overlaps everything) from having concurrent unmerged work: a queued job
that has no branch yet waits, with the persisted reason `shared paths: <job>` (shown by `status`), while an overlapping sibling is running or has a branch or pull request that is neither merged nor closed. A sibling that failed or
was blocked before it made a branch holds nothing. The loop treats a queued job whose reason is `shared paths: <job>` like a dependency wait, with two differences: the wait is released when that sibling's pull request is merged **or closed**
(or when the persisted state shows it holds nothing), and it never blocks the job. Each answered sibling is recorded in the `dependency-resumed` marker as `<job>@<sibling>`, so a chain of overlapping jobs resumes once per merge.

The polling is bounded by the interval (`FACTORY_MANAGER_TRIGGER_SECONDS`), the 20-pull-request cap per pass, one start per repository and pass, and the fact that a pass reads GitHub for a run only while that run
has a queued job waiting for a dependency.

**Failure and backoff.** A failed plan (non-zero exit, or no `plan.json`), a run that ended without ever writing its state, a refused start, or any GitHub failure keeps the
labels, logs **one** line and pauses that repository for 60 s, then 120 s, 240 s ... up to 1 hour; a success resets it: a started plan or run, or a whole pass that read GitHub and found nothing to
start (so a few blips over days do not add up to an hour's pause). The label stays, so a later cycle plans the issue again
under a new run name. There is no hot loop.

**Limits that still apply** (nothing here is a new power): the per-run budget and the guardrail (`run_budget_usd`; a top-up is refused once it is reached), the lease and the one-process-per-repo rule (the same `Manager._start`; the one append beside a supervisor is the only exception, and it takes no lease),
the command lanes, the per-invocation caps, `abandon_after` (a pending run is purged like any other idle one). The loop itself never merges anything, comments on, closes or edits an issue
and never reads issue text (the supervisors it starts post the outcome comment of `docs/operations.md`, "Comments on issues"; trigger-side outcomes such as a refused label get no comment yet): the GitHub reads are projected with `--jq` to numbers, label names, logins and role names before they leave `gh`, using the repository's own GitHub App
installation token (or the ambient `gh` login) exactly as the status labels do. The App needs `issues: write` (already required for the status labels; removing a label is the same call)
and read access to the repository metadata (the collaborator permission).

**Log lines** (all fixed-format, ids and counts only; the `skipped` lines are rate-limited to one change or one per hour):

```
trigger repo=org/name issues=2 run=auto-20270115T080000Z planning
trigger repo=org/name issues=2 run=auto-20270115T080000Z started
trigger repo=org/name issues=1 run=auto-20270101T000000Z started retry=yes
trigger repo=org/name issues=1 run=auto-20270115T080000Z appending
trigger repo=org/name skipped reason=busy|backoff|disabled|archived|config|slots_full|run_full|budget|draining|epic_cap|planner_spend_cap
trigger skipped reason=paused   (no repo: the whole trigger is paused; one line the first time, then silence)
trigger repo=org/name issue=7 skipped reason=unauthorised|ignored|in-flight|unremoved|backoff
trigger repo=org/name run=auto-... plan failed rc=1 code=planner_error backoff=60s
trigger repo=org/name issues=7,8 held code=planner_error failures=2
trigger repo=org/name hold comment failed error=<ExceptionType>
trigger repo=org/name hold comment skipped reason=<cap|cooldown>
trigger repo=org/name plan holds unusable reason=corrupt file=plan-holds.json
trigger repo=org/name plan holds not saved file=plan-holds.json
trigger repo=org/name run=auto-... append failed rc=1 backoff=60s
trigger repo=org/name run=auto-... run failed rc=1 backoff=60s   (also a failed dependency resume)
trigger repo=org/name run=auto-... retry refused issues=1 backoff=60s reason=cli-refused
trigger repo=org/name issue=7 retry replanned reason=dependency-closed
trigger repo=org/name run=auto-... jobs=1 started resume=dependencies
trigger repo=org/name run=auto-... job=fix-b blocked dependency=fix-a reason=dependency-closed
trigger repo=org/name run=auto-... job=fix-b block failed rc=1 backoff=60s
trigger repo=org/name run=auto-... skipped reason=dependency|over-budget
trigger repo=org/name run=auto-... job=fix-b skipped reason=backoff|resumed
trigger repo=org/name pass failed error=<ExceptionType> backoff=60s
trigger repo=org/name label-remove failed error=<ExceptionType>
```

The `code=` of a `plan failed` line says why the plan failed, as one word of a closed table: `plan` writes it to `plan-failure-code` in the run directory when it fails (the file holds the code and nothing else: no issue, pull request or model text, no exception message), and the loop reads it back. `invalid_plan_paths` (a job's `likely_paths` are not literal, canonical repository-relative paths), `schema_invalid` (the planner's result is not a valid plan), `planner_error` (the planner session failed or gave no usable result), `snapshot_failed` (the GitHub evidence could not be captured or read), `timeout` (the planner session hit its time limit), `other` (anything else; also what a missing, unreadable or unknown file reads as, for instance a plan killed before it could write one).

**Plan holds (`plan-holds.json`).** A plan that fails the same way again and again would be started again at every backoff. So the loop keeps, per repository, `${FACTORY_DATA}/repos/<org>/<name>/plan-holds.json` (beside `runs/`): for each sorted issue set it planned, the last `plan failed` code, how many plans in a row ended with that code, and a fingerprint (a hash) of the issues' labels and `updated_at` as they were when that plan started. A different code starts the count again at 1; a plan that ends well deletes the set's entry. Once the same code has occurred twice the set is **held**: the loop does not plan it, and logs `trigger repo=org/name issues=7,8 held code=<code> failures=<n>` once (not once per pass; a manager restart says it once more). The other issues of the repository are still planned (a held set takes no place in the `auto_build_max_issues` of the cycle). There is no timed retry: a hold lasts until a label of a held issue changes or the issue is edited (its `updated_at` moves), which allows **exactly one** more plan of the set; if that plan fails with the same code the set is held again with the new fingerprint. A plan a phase start (`epic_phases`) made is not counted here: it has its own guard.

The file holds only issue numbers, codes of the closed table above, counts and fingerprints: never issue text. It is written atomically (a temporary file next to it, `fsync`, rename), with at most 256 sets (the oldest go first), and re-read whenever it changes, so a restart keeps the holds. A file that is missing is an empty table. One that cannot be read, is larger than 256 KiB, is not JSON or is not of this shape is also an empty table, with one line `plan holds unusable reason=unreadable|oversized|corrupt` (an entry that is not valid is dropped the same way); the loop goes on and the next failed plan rewrites the file. A file that cannot be written is said once (`plan holds not saved`) and the hold is kept in memory until the manager restarts.

**Releasing a hold.** Change a label of the issue (add and remove one, for instance) or edit it: one more plan is allowed at the next pass past the backoff. To release it without touching the issue, delete its key from `sets` in `plan-holds.json`, or delete the file to release every hold of the repository; the running manager sees the change at its next pass, and no restart is needed. Do not edit the file to anything but removing keys.

**The comment a hold starts with.** When a set becomes held (the same code reached the threshold, here twice) Factory posts ONE comment on each issue of the set (`src/factory/hold_comments.py`, built on `IssueComments`), from its own identity: a hidden marker (`<!-- factory-outcome:v1 issue=<N> kind=plan_held -->`), the headline "planning is on hold", the failure code (one of `plan_failure.CODES`) and fixed text saying that **editing the issue or changing its labels** unblocks it (one more plan is then allowed). There is no issue title or body, no model text, no run name and no exception text in it; the body is `issue_comments.render_held`. It is posted once: before writing, the issue's comments are searched for a comment of Factory's own identity that starts with the marker, so a later failing pass (a hold that ends by an edit and begins again), a second manager and a restart find it and post nothing; found with another code, it is edited in place. `comment_on_blocked = false` in the repository's configuration and `FACTORY_ISSUE_COMMENTS=off` in the manager's environment post nothing, and the cooldown after three failures of `IssueComments` applies (a positive per-cycle cap is raised to the size of the held set for this one call, so a whole set is commented, but `FACTORY_ISSUE_COMMENTS_PER_CYCLE=0` posts none: the set is refused with the line `hold comment skipped reason=cap` and the hold itself is unchanged; a set stopped by the cooldown is one line `hold comment skipped reason=cooldown`). Closed issues and issues labelled `factory-ignore` get none. It is best effort: a failure is one line `hold comment failed error=<ExceptionType>` and never stops the loop or the hold. A comment moves the issue's `updated_at`, which is part of the hold's fingerprint, so after writing one the loop reads each commented issue once (labels and `updated_at`) and moves the hold's fingerprint over it, but only when the issue still had the labels and `updated_at` of the listing just before the comment, so a person's edit made while the plan ran still releases the hold; a label or an edit made after that releases it too. The comment stays after the hold is released; a later hold of the same issue edits or reuses it.

**What a hold covers beyond new plans, and what it does not.** (1) *Reads.* An eligible issue that belongs to a held set (its fingerprint unchanged) is skipped before its authorisation: it costs no GitHub call (not the label's actor, not their permission) and takes no place in the cycle, so an idle pass over any number of held issues makes the same few calls as one over none. The consequence: a held issue is no longer re-planned inside a *different* set (say, together with a newly labelled issue) until a label or an edit of one of the held set's own issues changes the set's fingerprint. (2) *Top-ups.* The same skip applies when the loop picks issues for `plan --append-to` into a running run, so a held set is never appended. A failed append (a non-zero exit, or no fragment for any of its issues) is counted like a failed plan, under the code `other` (an append leaves no closed failure code of its own): the same set failing twice in a row is held, besides the exponential backoff, until a label or an edit changes it; a good append deletes the entry. A top-up that fails to *start* (`append start refused`) is not counted: nothing ran, and only the backoff limits it. Releasing a hold is as above.

**Threat model.** A label is applied by a human with triage access, and triage is too little to spend money: hence the permission check on the person who applied it (the
event's actor, not the issue author), defaulting to `write`, and the refusal to go lower on a repository that anyone can read. A bot actor (`dependabot[bot]`, an Action using
`GITHUB_TOKEN`) never triggers. An issue body is untrusted text exactly as before: the trigger reads none of it, and the planner and the workers treat it as they always did (`docs/handoff.md`). The
label cannot be forged by editing an issue (the events are written by GitHub), but a user with `write` can of course start a build, which is the point; `factory-ignore` is the opt-out for
an issue, `auto_build = false` for a repository, `FACTORY_MANAGER_TRIGGER_SECONDS=0` for the deployment. Cost is bounded by `auto_build_max_issues` per cycle, one process per repository,
`run_budget_usd`, and the Anthropic workspace spend limit (`docs/operations.md`, "Cost guardrails").

### 5. Follow-up requests (the trigger pass)

After the label work of each repository, the trigger pass looks for **`@<app slug>` mentions on open pull requests the Factory App opened**, records each
new, authorised one as a *follow-up request* (phase 1, `src/factory/followup.py`: reading and recording only) and then turns the oldest queued request into a
**session on the pull request's own branch** (phase 2, `src/factory/followup_serve.py`, "Serving a request" below). It runs in the trigger loop, so
`FACTORY_MANAGER_TRIGGER_SECONDS=0` (or `FACTORY_MANAGER_OPERATIONS=off`) turns it off everywhere, `trigger-pause` pauses it, and it is independent of
`auto_build`. **It needs an identity to look for**: the bot `<FACTORY_GITHUB_APP_SLUG>[bot]`, or, for a deployment with no App identity (ambient `gh`), the
account named by `mention_handle`. With neither the pass does nothing and logs nothing. **It is off unless the repository says `followup = true`.**

> **Upgrading from phase 1.** Phase 1 shipped with `followup` on by default (it only recorded requests). From phase 2 on it is **off by default, because a
> request now starts a session**. A repository that has not set `followup = true` is no longer read at all: the already-deployed recorder pauses on every such
> repository until its configuration (for AKS the `repo_configs` entry, `infra/terraform.tfvars.example`) sets `followup = true`. Requests that were already
> recorded stay in their files and are served once the key is set. The default cooldown also moved from 600 to 300 seconds.

| Key | Default | Meaning |
|---|---|---|
| `followup` | `false` | `true` opts the repository in (reading, recording and serving). Anything but `true` is off. |
| `followup_reply` | `true` | With `followup` on, say on the pull request what became of a settled request: one comment, edited in place (phase 3, below). Anything but `true` is off; `FACTORY_ISSUE_COMMENTS=off` stops it everywhere. |
| `mention_handle` | `""` | For a deployment **without an App identity**: the account name that is mentioned (`@<handle>`), **and** whose pull requests are followed (a `User` account, not a bot). That account's own comments are always skipped (`skipped reason=self`) and never enter a session's context, so its replies cannot start a session. Letters, digits and `-` (at most 39). Empty means the App's slug. |
| `followup_min_permission` | `"write"` | The repository role the commenter must hold (`read` < `triage` < `write` < `maintain` < `admin`). Below `write` is **refused for a repository that is not private**, checked at every pass (one line `followup repo=... skipped reason=config`, once), exactly like `auto_build_min_permission`. |
| `followup_cooldown_seconds` | `300` | One request per user per pull request in this many seconds (0 to 86400; 0 means no cooldown). |
| `followup_max_sessions` | `5` | At most this many requests per pull request (1 to 100). Each queued request counts as the session it will become, at the moment it is queued: a request that is later refused still used its slot. |

**What is read.** Per pass and repository: the open pull requests, most recently updated first (5 pages of 100 at most; only those authored by the App are
followed), and for each one **that something happened on** the conversation comments, the review comments and the review bodies. A pull request's own
`updated_at` (any comment or review bumps it) is compared with the value at its last complete read: unchanged, and read less than an hour ago
(`RESCAN_SECONDS`), it is **skipped without a call and does not use the per-pass budget** (50 pull requests actually read per pass), so a quiet old pull request never
starves a busy one. When it is read, the conversation comments and review comments are asked only `since` the newest update seen (minus a minute of overlap, never
further back than a day), so a long thread is not read from its first comment on every pass; the reviews list has no such parameter, so all of it is read (30 pages
of 100). Hitting the page bound (10 pages per kind for comments) logs one `truncated kind=<kind>` line instead of silently missing the newest comments.
Pull requests that are closed or authored by anyone else are never read. Whether a comment mentions the App is decided inside `gh` (`--jq`): only the comment
id, the author's login and account type, the dates and that yes/no come back, so **comment text never reaches the manager**, is never logged and never stored.

**What counts as a mention.** `@<slug>` as a whole token, case-insensitively, in the text the person wrote *to the App*: `@Slug`, `(@slug)`, `@slug,`, `*@slug*`
and the emphasised `_@slug_` count; `me@slug.example`, `foo_@slug`, `@slug-two` and `@slug/team` do not. A **quoted line** (`> ...`), a **fenced block**
(` ``` ` or `~~~`, also one that is never closed) and an **inline code span** are cut out before the test: a maintainer who quote-replies to someone else's
`@slug ...`, or writes `` do not `@slug` this ``, has not addressed the App, so no request is recorded in their name.

Only the first 8,000 characters of a comment are searched (`MAX_SEARCHED_CHARS`): a 65 KB comment of hostile markup would otherwise make the `jq` filter take seconds, and a mention is at the start of what a person writes to the App.

**Age bound.** A comment *created* more than a day ago (`MAX_COMMENT_AGE_SECONDS`; its creation time, so an old comment edited just now to add a mention
is old too) is never decided, whether the pass first saw it on the first day after deployment or later: it is recorded as seen (a later edit of it is an
`edited`, never new) and one line `baseline comments=<n>` says how many. So deploying the feature does not turn a pull request's whole history into requests.

**Decisions.** Every comment not yet seen is decided once, in this order, and gets exactly one line:

| Outcome | When |
|---|---|
| `skipped reason=bot` | the author is a bot account (`type` `Bot` or a login ending in `[bot]`), Factory's own comments included |
| `skipped reason=self` | the author is the `mention_handle` account itself |
| `skipped reason=no-mention` | no whole-token `@<slug>` |
| `skipped reason=unauthorised` | the commenter's role from `GET repos/{r}/collaborators/{login}/permission` is below `followup_min_permission`, or the account has no role (404) |
| `skipped reason=cap` | `followup_max_sessions` requests are already recorded for the pull request |
| `skipped reason=cooldown` | the same user got a request recorded less than `followup_cooldown_seconds` ago |
| `request queued` | recorded |
| `skipped reason=edited` | an already decided comment came back with another `updated_at` and mentions the App; edits never trigger |
| `baseline comments=<n>` | `n` comments created more than a day ago were recorded as seen without a decision (see "Age bound") |
| `truncated kind=<kind>` | the page bound was reached for that kind of comment: the newest comments of that read were not seen |

A permission lookup that fails for any reason **fails closed**: no request is recorded, the comment is not marked as seen, and the next pass decides it
again (the failing pull request gets one rate-limited line `followup repo=... pr=N failed error=<ExceptionType>`; the others are still followed). Authorisation
is as for `factory-build`: the role is the collaborator permission, never `author_association`.

**State.** One file per pull request, `followups/pr-<N>.json` in the repository's data directory (`$FACTORY_DATA/repos/<org>/<name>/`), `0600`:

```json
{"version": 1, "pr": 12,
 "since": {"issue": "2026-11-07T10:00:00Z", "review_comment": "", "review": ""},
 "pr_updated": "2026-11-07T10:00:00Z", "read_at": 1794045605.0,
 "cursors": {"issue": 0, "review_comment": 0, "review": 0},
 "floors": {"issue": 0, "review_comment": 0, "review": 0},
 "fingerprints": {"issue:101": "2026-11-07T10:00:00Z"},
 "cooldowns": {"alice": 1794000000.0},
 "sessions": 1, "max_sessions": 5,
 "requests": [{"pr": 12, "kind": "issue", "comment_id": 101, "user": "alice", "permission": "write", "queued_at": "2026-11-07T10:00:05Z",
               "status": "done", "attempts": 1, "not_before": "", "started_at": "2026-11-07T10:01:00Z", "outcome": "pushed", "commit_before": "<40 hex>"}],
 "served": {"issue": 101, "review_comment": 0, "review": 0},
 "lock": null}
```

`since` is the newest `updated_at` read per kind (the next window), `pr_updated` and `read_at` the pull request's own `updated_at` and the time of its last complete
read (a failed permission lookup moves none of them, so the decisions are repeated); all three are optional in a file written before they existed.
`cursors` hold the highest decided comment id per kind (the three kinds number their ids separately), `fingerprints` the `updated_at` of each decided comment (the
3000 newest ids are kept), `floors` the highest id per kind whose fingerprint was forgotten (ids at or below it are old; anything above it without a fingerprint is new), `cooldowns` the time of the last request per user, `sessions`/`max_sessions` the counter and the cap, `requests` the durable
records (pull request, comment id and kind, the commenter's login and permission level, time). Phase 2 adds, per request, `status` (`queued`, `running`, `done`,
`refused`, `failed`; absent means `queued`), `attempts` (refused retries), `not_before` (the end of the pause after a refused one), `started_at`, a fixed `outcome` code
and the `commit_before` of the session, and per pull request `served` (the highest comment id of each kind that was handed to a session) and the `lock`
(`<run>/<job>/<comment id>` of the session in progress; `null` otherwise). A file written by phase 1 stays valid. The
file is replaced atomically (temp file in the same directory, `fsync`, rename), the directory and the file are opened without following symlinks and must belong
to the manager's user and not be group- or world-writable, and the size is capped at 256 KiB. A file that fails any of these checks (a symlink, another owner,
too big, not what the manager wrote) is **not** repaired or replaced: that pull request is skipped with the `failed` line until someone removes the file.
All decisions of a pull request are written in one atomic replace *before* their lines are logged, so a crash between reading and writing repeats the same
decisions (no request is lost or duplicated) and a crash after it repeats none. A comment that became visible with an id below the cursor (a pending review
submitted later) but above the floor and without a fingerprint is new and is decided like any other.

```
followup repo=org/name pr=12 request queued
followup repo=org/name pr=12 skipped reason=unauthorised|bot|self|cooldown|cap|edited|no-mention
followup repo=org/name pr=12 baseline comments=3
followup repo=org/name pr=12 truncated kind=issue
followup repo=org/name skipped reason=config
followup repo=org/name pr=12 failed error=<ExceptionType>
followup repo=org/name pass failed error=<ExceptionType> backoff=60s
```

The lines of phase 2 are listed under "Serving a request". The lines carry no login and no comment text. A pass costs the list of open pull requests (one call per 100), plus three reads (often one page each, thanks to
`since`) for each pull request that changed or is due for its hourly look, plus one permission lookup per distinct commenter with a new mention per pass; a
repository where nothing changed costs only the list.

#### Serving a request (phase 2)

After recording, the pass settles the session in progress and starts at most one queued request per repository (the oldest first). A session is **a retry of the job that
published the pull request**: the CLI's `retry <job> --reason="follow-up requested" --followup`, then `run`, so the supervisor syncs the branch to the base, the builder works
on the branch, and review, verification and publication run on a new head (`factory/gate` is posted again for that head only, as for any push; the pull request title and
body keep their issue reference). The manager entry is `Manager.retry_followup`, used in-process only: **there is no HTTP route** for it, and the `retry` action refuses the
`followup` field. The session limit is the repository's `auto_build_timeout_seconds`. A follow-up is also allowed on a `pr_incomplete` draft.

**Guards, in order.** Any failure of the first group leaves the request queued and logs a rate-limited `followup repo=... pr=N skipped reason=<reason>`:

| Reason | When |
|---|---|
| `paused` | the trigger pause is set |
| `busy` | a process of this manager works in the repository (the one supervisor per repository), or the repository lease is held elsewhere |
| `archived` | the repository is archived |
| `over-budget` | the run's cumulative cost (the original build included, `status --json`) has reached `run_budget_usd`; the request waits, with a pause that grows to an hour, and starts when there is room (at most until it `expired`, below) |

and the second group **refuses** the request with a fixed code, recorded in the state file (`status` `refused`) and logged once as
`followup repo=... pr=N refused reason=<code>`; a refused request is final, it is not retried, **no worktree is built and no new run is started**:

| Code | When |
|---|---|
| `run-gone` | no retained run holds the job that published the pull request (the run was purged) |
| `job-status` | the job is not `pr_ready` or `pr_incomplete` |
| `pr-closed` | the pull request is no longer open (or its job was merged or closed) |
| `head-moved` | the pull request's head on GitHub is not the commit the job retained: somebody pushed to the branch, used update-branch, or an auto-merge updated it. The supervisor never builds on a head nobody reviewed; a person asks again after the branch is back in step (or opens a new issue) |
| `not-authorised` | the author of the request no longer holds `followup_min_permission` when the session would start: the role is read again then, not only when the request was recorded (a lookup that fails keeps the request and backs off) |
| `expired` | the request waited for more than 24 hours without starting (a trigger pause, a busy or archived repository, a spent run budget): an old ask is never acted on |
| `cli-refused` | the CLI refused the retry three times (each refusal pauses the request 60 s, 120 s, ...; the open pull request, the base branch, the exact head and the context file are re-checked there) |

The documented deviation from the issue text: when the run is gone or the head moved, phase 2 does **not** start a new single-job run that adopts the existing branch and
pull request. That would need a plan entry outside the planner and a worktree built from a head that a person with write access may have pushed unreviewed.
This is not a rare case: a run with no live worker is purged after `abandon_after` (default `7d`) of inactivity, an open pull request or a job waiting in `pr_ready` does not
keep it, so with the default **a mention on a Factory pull request that has been idle for a week is refused `run-gone`**; a person raises `abandon_after` for the repository
or opens a new issue. Adopting the branch of such a pull request is not built and needs a design of its own.

**The lock.** Before anything starts, the pull request's state gets `lock = <run>/<job>/<comment id>` and the request `status = running`, so a crash or a second pass cannot start a
second session for the pull request. While a lock is held no other request (of any pull request) starts. A refused retry releases it again (the request is queued with `not_before`);
a lock whose retry never took effect (a crash between the two) is released with `followup ... requeued reason=retry-not-applied` and the request is served again. A lock whose job is
`queued` or `waiting_*` (the `run` did not start: busy lease, shutdown, a manager restart) is finished by starting its `run` on a later pass, with a pause between refused starts
(`followup ... start deferred status=<n> backoff=<s>`). That start follows the restart recovery's budget rule (a run that already spent `run_budget_usd` is not started
again: `followup ... skipped reason=over-budget`, with the same growing pause) and is made at most three times per request, with a pause that doubles after each start (60 s, then 120 s; the pass after the third start gives the session up, so about five minutes in all), so a
supervisor that cannot take hold (the token broker or the environment is down for a few minutes) is outlived. A lock whose job is `running` or `interrupted` with no
process is left to the restart recovery (`auto_resume_after_restart`, which resumes the same session: the follow-up record stays on an interrupted job while the context file is still
there; once it is gone, a `retry` of that job is an ordinary one). Either kind of lock is released as
`session-lost` once an hour has passed since the session started and its job still has no process, or once three restarts of its `run` were made and the job still has no process, so a lock that can never be finished does
not hold up the other pull requests of the repository.

**What a `session-lost` leaves behind.** Before the manager releases the lock of a job that is still `queued` (or `waiting_build`) on the commit it retained, it runs the CLI's
`followup-release <job>` (under the repository lease, so only while the lease is free; `followup ... release failed backoff=<s>` when the command failed, at most three times, after which the lock is
released anyway): the job goes back to the `pr_ready` or `pr_incomplete` status and phase it had and its follow-up record is dropped. Nothing was built, so **nothing is lost**: the next mention is
served as it would have been, and the job never sits in `queued` behind a context file that is gone. A job that already built a commit (`waiting_review`, `waiting_verify`, `waiting_publish`)
is left as it is: the work is in its worktree, and an operator's `run` carries it through review, verification and publication. The context file is required only for the first build round: a later round (review asked for a fix) carries on without it, since the first round already acted on the request. If a
`followup-release` could never run (three failures), a job left `queued` with a `followup` record is put right by hand: `block <job> --reason=...` then `retry <job> --reason=...`, or purge the run.

**The outcome** of a session, read from `state.json` when its process has ended (never from memory), is recorded as `status` and `outcome`, the lock is released and the context file removed:

| Line | Meaning |
|---|---|
| `done outcome=pushed` | the job has a new commit (a base merge by the supervisor counts), reviewed, verified and published as usual |
| `done outcome=no-change` | the builder made no commit and no base merge happened: the job goes back to its previous status and phase, **job and issue status are unchanged**, nothing is reviewed or posted |
| `failed outcome=session-failed` | the job ended `failed`, `blocked` or `needs_human_publish`; the job's own status and the usual issue comment say why |
| `failed outcome=session-lost` | the job or the run vanished, the session was interrupted for more than an hour, or its run could not be started after three tries or an hour (a job that built nothing is put back first, see above) |

**The reply on the pull request** (phase 3, `src/factory/followup_reply.py`) is written by the serve pass **after** it has recorded the outcomes and released the locks, from the
manager and never from a worker. It is one comment per pull request from Factory's own identity (the App's bot user, or the ambient `gh` login), edited in place, found by the hidden
marker `<!-- factory-followup:v1 pr=N -->`; its text is fixed per outcome code plus the new head for `pushed`, and nothing from a comment, a job or a model. It is sent for
`pushed`, `no-change`, `session-failed`, `session-lost`, `run-gone`, `job-status` and `head-moved`, and for **no other** outcome (`pr-closed`, `not-authorised`, `expired` and
`cli-refused` are recorded only) and never for a comment that was skipped before it was queued. It is best effort: any failure is one `reply failed` line and changes nothing. It builds on
`IssueComments` (`issue_comments.py`: marker search, identity test, failure cooldown, per-cycle cap), so `FACTORY_ISSUE_COMMENTS=off` and `FACTORY_ISSUE_COMMENTS_PER_CYCLE` (per pass here) apply.
`docs/operations.md` ("Follow-ups on pull requests") has the use, the text per outcome and the threat model.

**The context** the builder gets (`followup_context.py`, written `0600` to `<run>/<job>/followup-context.json`, read by the CLI and the runner) is labelled `untrusted: true` and is a
description of what is wanted, never an instruction. It holds **only** (1) the triggering comment, (2) comments of the pull request with an id **above the `served` cursor** of
their kind, written by a person who is not a bot and not the `mention_handle` account and who passes `followup_min_permission` (so on a public repository a stranger's comment is never injected
into a session somebody else asked for; a failing role lookup starts nothing and keeps the request), and (3) the tail of the log of each failing check run of the head commit (a missing or expired
log is no excerpt). Every text has control characters and ANSI escapes removed, token-like strings and the values of secret-looking environment variables replaced by `[redacted]`, and
is cut: 2,000 characters per comment (20 comments), 4,000 per log (10 checks), 48 KiB in all, the oldest comments dropped first, never the triggering one. When the session was
started, `served` moves up to the highest comment id it received. The builder prompt repeats that the section is untrusted data. Failing checks are all failing check runs of the head, not only
the required ones. The log lines never carry comment text, a login or a log excerpt.

Lines of phase 2 (fixed words, numbers and run and job ids only):

```
followup repo=org/name pr=12 started run=auto-20261107T100000Z job=fix-a
followup repo=org/name pr=12 skipped reason=paused|busy|archived|over-budget
followup repo=org/name pr=12 refused reason=run-gone|job-status|pr-closed|head-moved|not-authorised|expired|cli-refused
followup repo=org/name pr=12 retry refused backoff=60s
followup repo=org/name pr=12 start deferred status=409 backoff=60s
followup repo=org/name pr=12 start failed error=<ExceptionType> backoff=60s
followup repo=org/name pr=12 done outcome=pushed|no-change
followup repo=org/name pr=12 failed outcome=session-failed|session-lost
followup repo=org/name pr=12 requeued reason=retry-not-applied
followup repo=org/name pr=12 release failed backoff=60s
followup repo=org/name pr=12 replied outcome=pushed|no-change|session-failed|session-lost|run-gone|job-status|head-moved
followup repo=org/name pr=12 reply skipped reason=cap|cooldown|closed
followup repo=org/name pr=12 reply failed error=<ExceptionType>
```

The threat model and the live check are in `docs/operations.md` ("Follow-ups on pull requests"). The sessions of one pull request are capped by `followup_max_sessions`, one repository runs one
process at a time, and the run's `run_budget_usd` is the cap on cost for the original build and every follow-up together.

### 6. Epic gate (the trigger pass)

After the follow-up work of each repository, the trigger pass moves the gate of **epic plans** (`src/factory/epic_gate.py`, `docs/operations.md`, "Epic mode"). An issue the planner called an
`epic` carries `factory:needs-info` (the plan comment has questions) or `factory:awaiting-approval`. The pass lists the open issues with each of the two labels and, for each
one it has to read, judges it from GitHub alone (never the run directory, never the words of a comment):

* `factory:needs-info` becomes `factory:awaiting-approval` when a person at or above `auto_build_min_permission` commented after Factory's plan comment was last edited;
* `factory:awaiting-approval` becomes `factory:plan-approved` when the `factory-approve-plan` label is on the issue now, its newest applier is such a person, and it was applied after the plan comment's last edit
  (and, when the plan had questions, after the first authorised answer);
* bots (Factory's own comments included), people below the minimum, a lookup that failed, a missing plan comment, more events or comments than the page bound, `factory-ignore` and a closed issue change nothing.
  The permission lookups per issue are bounded (10 distinct commenters, then only owners, members and collaborators as GitHub labels them, up to 40), so drive-by commenters cannot hide a maintainer's answer; when a bound still hides it, the pass logs `truncated` (below).

The new label is added before the old one is removed, so an issue never has none. The gate half writes nothing else: no comment, no plan, no run, no pull request. The phase half (`docs/operations.md`, "Phase issues and the status comment") opens the phase issues, keeps the status comment and, from slice 3, STARTS the build of the open phase itself: `Operations._start_phase`, called by `_work` (so after every precondition of a plan and after a person's labelled issue) when nothing else is to start, asks `epic_phases.prepare_start` to prove the approval and the ledger again from GitHub, writes the attempt to the status comment, puts the stamp `epic-phase` beside the `trigger-run` marker in the new run directory (written and removed together, `_clear_run`) and starts `plan --issues=<phase issue>`, which reads the stamp's verified text instead of the issue. At most one start per repository per pass; `epic_max_open_prs` holds it (`skipped reason=epic_cap`); a stalled phase moves the parent to `factory:blocked`. It never applies the trigger label. It is ON by default for every repository the manager watches; `epic_mode = false`,
`FACTORY_EPIC_MODE=off` (deployment-wide) and `auto_build = false` switch it off, with no GitHub call and no output. `FACTORY_MANAGER_TRIGGER_SECONDS=0`, `FACTORY_MANAGER_OPERATIONS=off` and `trigger-pause` stop it like the rest of the trigger pass.
A repository the manager refuses to start work in (archived, or an `auto_build_min_permission` below `write` on a repository that is not private) is skipped with the usual `trigger repo=... skipped reason=archived|config` line.

Pacing: two listings per repository per cycle; an issue is read when its `updated_at` changed or an hour passed, at most 20 per pass; an issue in `factory:awaiting-approval` without the approval label costs no further call; one
failing issue is reported and read again next pass without stopping the others; a failing pass backs the repository off (60 s, doubling, an hour at most). One fixed line per move and per failure, never text:

```
epic repo=org/name issue=7 state=awaiting-approval
epic repo=org/name issue=7 state=plan-approved
epic repo=org/name issue=7 failed error=CalledProcessError
epic repo=org/name issue=7 truncated
epic repo=org/name pass failed error=CalledProcessError backoff=60s
```

Re-applying the trigger label to an issue with either waiting label is a retry request (`trigger.RETRY_STATUS_LABELS`): the old run has no job for it (`operations.NOT_BUILT` holds `epic`), so it is planned afresh in a new run and the plan
comment is edited in place. `factory:plan-approved` is a retry label too (`RETRY_STATUS_LABELS`) but an approved epic is planned afresh only while none of its phases is in flight (`skipped reason=in-flight`; anything unreadable counts as in flight). The phase issues are in the next subsection; see "Epic mode" in `docs/operations.md` for what is not done yet.


**Phase issues (the second half of the pass; `src/factory/epic_phases.py`, `docs/operations.md`, "Phase issues and the status comment").** Where epic mode is on AND Factory may comment
(`comment_on_blocked`, not `FACTORY_ISSUE_COMMENTS=off`), the pass then lists the open issues with `factory:plan-approved`. For each parent that is due it re-derives, from GitHub alone, that the approval
still stands (`epic_gate.standing`: the gate's decision asked again; the label only finds the parent), finds Factory's status comment (marker and Factory's identity) and the phase issues (the ledger's numbers united with a
bounded listing of the issues Factory's own identity opened since the plan comment's last edit; author id, marker and the plan's digest, never a label), and decides (`epic_phases.decide`, pure): open phase 1, or phase k+1 once phase k is
merged (the phase issue closed AND a closing pull request of this repository merged: `GithubReader.closing_pulls`, a fixed GraphQL query for numbers and states, then `pull_state`), or report. It opens at most one phase issue per
parent per pass (never more than the plan has phases), writes it right after a fresh search by marker, and brings the status comment (checklist, state, code, hidden ledger) up to date. It never applies the trigger label and never edits the plan comment, and the pass itself starts nothing: a halt, a duplicate or a deleted phase issue is only REPORTED in the status comment and in one rate-limited log line, and only a STALLED phase
(started, with no pull request and no run that can still produce one) relabels the parent, from `factory:plan-approved` to `factory:blocked`. The manager's label pass starts the open phase when nothing else is to start (`operations._start_phase`, `epic_phases.prepare_start`;
the approval is proven again from GitHub at the plan's start and again right before its build, `operations._epic_build_proven`, and before a restart's auto-resume or a dependency resume of that run, `operations._epic_verdict`; `docs/operations.md`, "Automatic start"), and a person can still apply the trigger label to a phase issue to build it by hand, exactly as for any issue; the pull request links are below. Nothing is remembered that the next pass needs to be right: a restart loses only the round-robin cursor and
the "already read" cache. Log lines: `epic repo=org/name issue=7 phase=1 child=201 created`, `epic repo=... issue=7 state=halted code=closed-unmerged`, `epic repo=... issue=7 state=stopped code=duplicate-children`, and failures as above
(`failed error=<Type>`, `truncated`; a status comment that could not be written is `failed error=StatusFailed`). A search for the next phase issue that ran out of pages (5 pages of 100, newest first) opens nothing, logs `truncated`
and shows `unknown` / `listing-truncated` in the status comment. A stopped, halted or unknown epic is read again within an hour. What a person can do: for `duplicate-children`, keep one phase issue and delete the first
line (the hidden marker line) of the other, or have an administrator delete it (closing it does not help: closed phase issues still count); for `closed-unmerged`, reopen the phase issue (closing a phase issue is what makes the
halt, so never close one to resolve it). For `child-deleted`, `listing-truncated` and a phase issue closed on purpose nothing resumes the epic by itself: the remaining phases are opened and built by hand. Deleting the status
comment is not a remedy (it is the ledger). Details: `docs/operations.md`, "Getting an epic unstuck".

**Pull request links and the nested-epic block (slice 2; `epic.py`, `epic_phases.py`, `runner.py`, `completion.py`, `cli.py`).** Three places meet the phase pass without being part of it, and all of them decide from GitHub, never from a run-directory file:

* *Plan time* (`plan` and `plan --append-to`, `cli.phase_checker`): every captured issue that looks like a phase (the origin label, or the marker as the first line of its body) is read once with Factory's own identity (`GithubReader.phase_issue`,
  one `GET issues/N`); it is a phase only when its author id is Factory's. A phase gets a stamp on its job (`epic_phase`: parent, child, phase, of, plan digest; validated by `model.validate_plan` and `fragments.parse`), a planner's `epic` for it is
  `blocked` with a fixed reason (`epic.downgrade(..., nested=)`), and a job that groups it with other issues is blocked with its dependents (`epic.bind_phases`). No GitHub call is made for an issue that does not look like a phase, nor where epic mode or
  label triggers are off.
* *Publish and completion* (`Runner.phase_link`, `completion.complete_verified(links=)`, called by `reconcile`): `epic_phases.phase_link` builds the `epic.PhaseLink` of a job that is exactly one phase issue and `epic.first_line` writes the line.
  The proof for `closes #<parent>` (last phase only) is `read_parent` again: the parent open, not ignored, both labels, `standing`, the same digest as the stamp, every earlier phase merged, the phase issue the only valid child of its phase. It runs at every publish
  and every completion, fails safe to `refs`, and never raises. A rewrite by `factory-verified` accepts all three lines Factory writes (`refs`, `Closes #c, refs #P`, `Closes #c, closes #P`) and writes the live one.
* *The phase pass* (`_settle`): a halted or stopped epic, and the first read of a plan whose digest differs from the ledger's, rewrite the first line of an open pull request that closes one of the phase issues from `closes #<parent>` to `refs #<parent>` (`_demote_all`, one
  `closing_pulls` and at most one `pull_facts` and `set_pull_body` per open pull request). For an old plan it runs before anything else is written (only for the unmerged phase issues that still exist and are Factory's, `_live_children`, as in `_withdraw`; a deleted one is never asked about, so its open pull request is not found and keeps `closes #<parent>`: not covered, `docs/operations.md`) and keeps the old ledger when a call fails; for a halt it runs after the status comment. Log line: `epic repo=... issue=7 pull=500 parent link demoted`.
* *A parent the pass cannot prove* (`_withdraw`, `_sweep`): a parent with `factory-ignore`, without `factory-approve-plan`, with an approval that does not stand, without a valid plan comment, or with the origin label `factory:epic-phase` forged onto it (never read or settled, so no label on a parent can keep a `Closes #<parent>` alive) is not settled, but the unmerged phase issues of its ledger (the status comment is read
  without `standing`; a deleted or foreign one is left out, and the pull request of a deleted one is not found) go through `_demote_all` too (one cheap drop each, retried after `REREAD_SECONDS` when a call failed). A parent without `factory:plan-approved` is never listed, so `_sweep` lists the open `factory:epic-phase` issues (one call
  per pass), reads each one's marker once (`PhaseState.parents`) and demotes the pull requests of a phase issue whose parent is open and lacks the label (at most `MAX_SWEPT_PER_PASS` per pass). A pass that does not run (a kill switch, a pause) and a read that fails or stops at a bound demote nothing.

The gate (`epic_gate.state_of`) answers no state for an issue that carries `factory:epic-phase`, and the phase pass never reads or settles such an issue as a parent (it only runs the withdraw above for it). No new GitHub App permission; no new setting.

### Limits worth knowing

* Restart recovery runs `recover` on every run with a `state.json`, and the CLI rewrites
  `state.json` whenever it opens a run, so a restart restarts the idle clock of the runs it
  recovered: abandonment is measured from the last restart at the earliest.
* The dashboard endpoints and the history ledger add no GitHub call: they read files, bounded as listed under "Dashboard endpoints". The
  ledger's reads, writes and the janitor's prune are local and best effort.
* `status --json` asks GitHub about each open pull request, so the guardrail makes one such
  call per budgeted run per `FACTORY_MANAGER_POLL_SECONDS` and the sweep one per run per
  `FACTORY_MANAGER_JANITOR_SECONDS`.
* The epic gate pass adds two issue listings per watched repository per trigger cycle and reads an epic issue only when its `updated_at` changed (or hourly), at most 20 per pass (section 6). The phase half adds one listing (the approved parents), reads at most 10 approved parents in full per pass (about 9 calls each, again after 2 minutes while a phase is in progress; a read that fails or ends with an approval that does not stand counts as one) and at most 30 cheap drops (one call for a parent without a plan comment, one more per further 100 comments on it, at most 10 pages), and writes at most `FACTORY_ISSUE_COMMENTS_PER_CYCLE` status comments per pass.
* The dependency-merge resume reads one pull request per unmerged dependency of a waiting job (20 per pass at most) and only for runs that have such a job.
* The label watcher makes up to two GitHub reads per watched repository per `FACTORY_MANAGER_TRIGGER_SECONDS` (the repository and the labelled issues; more only when an
  issue needs its actor looked up, for at most `auto_build_max_issues` new and as many retry issues per cycle, plus the refusals not yet remembered), and none while a plan or append of the repository is running, its lease is busy, or a supervisor's run has no `state.json` yet. With a supervisor running it reads the same (plus the run directory, locally) whenever a build slot is free or a fragment is waiting in the inbox; a pass that finds the run full, over budget or draining reads nothing unless a fragment is waiting.
* The threads are not daemons. Shutdown sets the event that every wait uses, closes the runner
  (which interrupts the commands in flight) and joins them for up to 5 s after the children are
  gone; a thread that is still alive then is reported on stderr. A trigger pass that sits in a
  `gh` call (30 s at most) notices the shutdown between calls, so that report can appear; it is
  cosmetic, because every start is refused with `503` once shutdown began.

## Shutdown

On SIGTERM or SIGINT the manager stops accepting connections, refuses new starts (`503`),
sends SIGINT to the process group of every plan/run child it started that was not already
interrupted by a `pause`, and to every short command in flight (so the CLI can release its lease
and stop cleanly), waits up to `FACTORY_MANAGER_SHUTDOWN_SECONDS` (default 25) for both, then SIGKILLs
the groups that are still alive, joins the operations threads (up to 5 s more), wakes handler
threads blocked on slow clients and exits `0`.
Commands requested after shutdown began get `503`. Children release their own leases on the way out; a supervisor that
is killed instead of interrupted cannot, and its lease simply expires. One line goes to
stderr if anything had to be killed. Keep the container's termination grace period above
`FACTORY_MANAGER_SHUTDOWN_SECONDS` plus a few seconds. Prefer `drain` before a planned
rollout.

## Audit line

Exactly one line per request on stdout, written before the response is sent:

```
audit principal=<id> method=<M> path=<path> status=<code> argv=<json list> [bytes=<n>]
```

* `principal`: `easy-auth`: object id claim (`objectidentifier`, `oid` or the long Microsoft claim URI),
  else the name claim, else `unknown`. `entra-jwt`: `preferred_username`, else `azp`, else `oid`, else
  `unknown`. `anonymous` when authentication failed (including `503`).
* `path`: the request path without the query string, as sent (not decoded).
* `bytes`: only on a successful log read: the number of bytes of log text in the answer. Never the text.
* `argv`: the CLI argv that was executed, as a compact JSON list; `[]` when nothing ran
  (every `401`, `403`, `404`, ..., every rejected body, every dashboard read and every log read). Every `--reason=` element is
  replaced by `--reason=<N chars>`, and so is `--principal=` of a stop. For the file endpoints the argv names the inbox path
  (a digest), never the content. Request bodies, reasons and CLI output are never logged. `POST /trigger-pause` runs no CLI;
  its argv is `["trigger-pause","on"]` or `["trigger-pause","off"]` when it succeeded.
* Control characters, spaces, `=`, backslashes and non-ASCII characters in `principal`,
  `method` and `path` are written as `\xNN`/`\uNNNN` escapes, and each field is cut at 200
  characters (128 for the principal) with `...`, so a request cannot forge or split a line.
* `GET /healthz` answering `200` is not audited (probes would flood the log). A `/healthz` refused with `401` or `503` is. A request for the browser page's shell is audited like any other, as `principal=anonymous` and the path without its query.

```
audit principal=<<OBJECT_ID>> method=POST path=/repos/<<ORG>>/<<REPO>>/runs/r1/retry status=200 argv=["/usr/bin/python3","-m","factory","--repo","<<ORG>>/<<REPO>>","--run-dir","/data/repos/<<ORG>>/<<REPO>>/runs/r1","retry","fix-a","--reason=<27 chars>"]
audit principal=anonymous method=GET path=/repos status=401 argv=[]
```

The only other routine stdout line besides the startup and operations lines is the GitHub check's
`github repo=<org/name> status=<value>` (a change of a repository's `github` value in `GET /repos`;
the value is one of the fixed strings listed there and carries no secret or broker output).

Anything the manager must report about itself (a child that could not be signalled, a
repository configuration that does not load, an unexpected exception) goes to stderr as
`factory-manager: <message>` and names the exception type only, never its message,
request data or CLI output.

## Status codes

| Code | When |
|---|---|
| 200 | CLI exit 0; `/healthz`; `/repos`; the dashboard endpoints; the files of the browser page and `/ui/config.json` |
| 307 | `GET /ui`, to `/ui/` |
| 202 | plan or run child started |
| 400 | invalid JSON, unknown or mistyped field, bad `job`/`phase`/`reason`, bad run name, bad `Content-Length`; a dashboard endpoint: a bad job name or an invalid query parameter (`{"error":"invalid query parameter","parameter":...}`) |
| 401 | `easy-auth`: missing or malformed `X-MS-CLIENT-PRINCIPAL`. `entra-jwt`: missing, malformed or invalid bearer token, with `WWW-Authenticate: Bearer` (one uniform answer; also `/healthz` from a non-loopback peer without a valid token). Everything except `/healthz` (loopback) and the files of the browser page and `/ui/config.json` |
| 403 | valid principal without the needed role; `stop`: a request a page of another origin sent |
| 404 | unknown path; repo not allowlisted or malformed (identical body); `run not found` (inbox); `run has no plan` |
| 405 | wrong method (`Allow` header set) |
| 408 | body not received in time (30 s total) |
| 409 | plan/run: a child is already running for the repo, lease held elsewhere, run already planned; pause: no child; stop: the job is not running, or no supervisor is running the run |
| 411 | chunked request body |
| 413 | body over the cap |
| 415 | `stop`: no `application/json` content type |
| 422 | CLI exit non-zero |
| 500 | repo config does not load; internal error; the CLI could not be started |
| 503 | lease backend unavailable; manager shutting down; too many commands in progress (`Retry-After: 5`); `entra-jwt`: the token signing keys cannot be obtained, so the token cannot be verified (`Retry-After: 5`, body `authentication unavailable`) |
| 504 | short command timed out; pause timed out |

## Deviations from the first design

1. **Lease ownership** (above): the supervisor owns it, the manager probes.
2. `resume` accepts `review`, `verify`, `publish`. The first design listed `build` too, but
   `factory resume --phase` has no `build` choice, so the manager rejects it with `400` instead
   of letting the CLI fail later. `tests/test_manager.py` runs every argv it can emit through
   `factory.cli.build_parser()` so the two cannot drift.
3. `record-capabilities` takes a JSON **array** (the CLI's loader requires a list); the other two
   take an object. `import-evidence` has no job in the URL, so the job is the body's `job_id`
   (the CLI checks it against the manifest).
4. `reason` is optional for `resume` and `retry`, as in the CLI (`retry` of a job that has already
   run needs one; the CLI says so).
5. Non-zero CLI exit is `422`, a timeout `504`.
6. Extra environment variables: `FACTORY_MANAGER_PAUSE_SECONDS`, `FACTORY_MANAGER_REQUEST_TIMEOUT`, `FACTORY_MANAGER_MAX_STATUS_COMMANDS`, `FACTORY_MANAGER_MAX_COMMANDS`, and for the operations loop `FACTORY_MANAGER_OPERATIONS`, `FACTORY_MANAGER_LEASE_WAIT_SECONDS`, `FACTORY_MANAGER_POLL_SECONDS`, `FACTORY_MANAGER_JANITOR_SECONDS`, `FACTORY_MANAGER_TRIGGER_SECONDS` (label-triggered builds), and for authentication `FACTORY_MANAGER_AUTH`, `FACTORY_ENTRA_TENANT_ID`, `FACTORY_ENTRA_AUDIENCES`, and for the browser page's sign-in `FACTORY_ENTRA_SPA_CLIENT_ID`, `FACTORY_ENTRA_SPA_SCOPE` (the `entra-jwt` mode exists because AKS has no Easy Auth).
7. Pre-checks on `plan`/`run` (already planned, no plan) answer immediately instead of starting a
   process that would only write the same error to its log. The CLI stays authoritative.
8. `pause` also interrupts a `plan` child this manager started (it is the same mechanism).
9. `/repos` does not list lease holders; it lists this manager's own children. The restart logic
   waits for the lease to be free by probing it (Operations loop); it never reads the holder.
10. An unauthenticated request to an unknown path is `401`, not `404`.
11. **The dashboard endpoints do not shell out.** Every other endpoint is the argv of one CLI command; the four dashboard `GET`s read
    `plan.json`, `state.json` and the ledger in the manager's own process (`status_report` with no pull request lookup, which is
    read-only), because a dashboard that ran `status --json` per page view would call GitHub for every open pull request each time
    and fill the `status` lane. They never write, signal a process or call GitHub. Their argv in the audit line is `[]`.
12. The manager writes to the history ledger itself (an `operator` entry for each successful `POST`, and the `pr_observed` and `purged`
    entries of the operations loop); everything else is written by the supervisor and the CLI.
13. Extra environment variables: `FACTORY_HISTORY_MAX_BYTES` and `FACTORY_HISTORY_RETENTION_DAYS` (shared with the CLI).
14. **The static shell of the browser page is served without a token** (`GET /ui/`, `/ui/app.js`, `/ui/style.css`, `/ui/vendor/msal-browser.min.js`, the sign-in settings `/ui/config.json`, and the redirect `GET /ui`), the one exception to item 10 besides `/healthz`. A browser navigation cannot send a bearer header and the page must load before it can ask for one; the files carry no data and no secret (the settings are the public identifiers every sign-in URL carries), and every data call the page makes still needs a valid bearer token (`401`, `403`). It is an exact-string allow-list; every other path under `/ui` is an unknown path (`401` without a token). The acceptance line "an unauthenticated request to any page returns 401" is read as "to any page or call that shows data". See "The browser page".
15. **The browser page shows, and signs in with Microsoft only in `entra-jwt` mode.** (Its one action, the stop of a job, is described under "Stop" above.) Phase 5 of the dashboard issue replaced the pasted token by an MSAL redirect sign-in behind the same four-call seam; the manager still verifies the bearer token and the app roles itself, exactly as for any other client. Deviations from the issue's text: (a) the pasted token is **kept only in `easy-auth` and local mode**, where there is no browser sign-in and the platform in front is the trust root (it gets data only behind a front that turns it into the principal header: `easy-auth` ignores `Authorization`); in `entra-jwt` mode it is removed, so there is one way in and no paste field to social-engineer; (b) the page uses **no popup and no hidden frame** (renewal is a refresh-token request, then at most one guarded redirect), and needs **only `https://<host>/ui/`** as a registered redirect URI (the page sets its own `redirectUri`, so the host root is not needed and is not listed); (c) `Strict-Transport-Security` is sent only for a request the proxy says was HTTPS, from a private peer ("The browser page"); (d) the client id and scope the page needs are derived from `FACTORY_ENTRA_AUDIENCES` unless `FACTORY_ENTRA_SPA_CLIENT_ID` / `FACTORY_ENTRA_SPA_SCOPE` say otherwise. What no offline test can show (the real sign-in, the registered redirect URIs, the `roles` claim in a single-page-application token, the certificate of the custom host) is listed under UNVERIFIED in `infra/README.md`.
16. **Logs are served, to Operators only** ("Log endpoints"): the earlier rule that no log is ever returned is replaced by an allow-list, no-link opens, plain text, redaction and bounded ranges. A retained, redacted copy of a purged run's logs is kept for `FACTORY_LOGS_ARCHIVE_RETENTION_DAYS` (default 30) and is not served ("Log retention"); a failed copy never stops a purge.

## Seams

`Manager.children` is the registry of started processes; `Manager.runner` and `Manager.spawner`
are the only two ways a CLI process is started. `Manager.run_short` (the lane slots and the
runner), `Manager.lease_state` (the lease probe), `Manager.start_run`, `Manager.start_plan` and `Manager.retry_job` (the `POST .../run`, `.../plan` and `.../retry`
paths) are shared by the HTTP handlers and the operations loop, so the loop shares the lanes, the
spawner, the child registry and the lease probe with them (it additionally issues `recover` and `purge-clone`, which have no endpoint). `Manager.housekeeping()` only reaps finished children; the long loops
live in `operations.py`. Nothing is global: `Manager`, `Children`, `Sink`, `Operations` and the
server are built in `main()` / `serve()`.
