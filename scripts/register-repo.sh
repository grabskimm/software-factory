#!/usr/bin/env bash
# Register ONE repository (same organisation) with Factory in MANUAL-MERGE mode.
#
# Manual mode: Factory plans, builds, reviews, verifies and opens ready pull requests with a
# factory/gate status; a HUMAN merges. There is NO enrolment workflow and NO ruleset, so human
# pull requests are never blocked. The only things this takes are the Factory App on the
# repository and a repo_configs entry in the INFRA_TFVARS variable of the Factory repository.
# (The full auto-merge hand-off is scripts/enroll-repo.sh; docs/enrolling-repositories.md says
# which of the two to use.) DEFAULT IS A DRY RUN: nothing is written without --apply.
#
# A registered repository is WATCHED by the manager: an issue labelled factory-build by someone with
# write access is planned and built without any API call. --no-auto-build opts the repository out;
# --auto-build-label, --auto-build-concurrency, --auto-build-max-issues and
# --auto-build-min-permission tune it (docs/manager.md, "Label-triggered builds").
#
# This is a sibling of enroll-repo.sh and not a --mode of it on purpose: it needs none of that
# script's options (CI checks, merge-enrolment App, ruleset edits, workflow commits), it has a
# different set of refusals, and enroll-repo.sh stays byte for byte what it was. The small
# helpers (do_write, say, die, ...) are copied, not sourced, so each script stands alone.
#
# Rules this script keeps (do not "improve" them away):
#   - every change goes through do_write; dry run prints the command and the diff only;
#   - it prints only a diff of the repo_configs region of INFRA_TFVARS, never another variable
#     and never any other part of that variable;
#   - the variable is read twice and written only if the second read equals the first;
#   - it never dispatches a workflow, approves an environment, drains a run or merges anything;
#   - no token or secret is read, printed or put in an argument list.
#
# Exit codes:
#    0  done, dry run finished, or nothing to do (already registered). Also 0, with nothing
#       written: an entry for the repository exists with DIFFERENT content and --replace-config
#       was not given ("NOT overwritten" is printed). A caller that needs to tell "registered as
#       asked" from "left as it was" reads the output (NO-OP / "differs"), not the exit code.
#    1  a gh/curl call or an unexpected condition failed; also: INFRA_TFVARS changed between the
#       first read and the write (re-run), or --verify-url could not be answered
#    2  usage error or an invalid value (rejected before any gh call)
#    3  preflight refused (auth/scopes, repository missing/archived/foreign/fork/empty, Factory
#       App not installed in the organisation, INFRA_TFVARS missing or without exactly one literal
#       repo_configs map, result over the 48 KB variable limit, an edited text that does not
#       verify locally). Every one of these is decided BEFORE the first write.
#    4  refused because manual mode is meaningless here: the repository is already matched by the
#       hand-off ruleset (or its default branch already requires factory/gate)
#    5  verification failed: the written variable does not show the entry (or cannot be read or
#       parsed again after the write: fix it by hand), or --verify-url found the repository
#       listed with an error (or absent although the entry was already in place)
#   10  manual step needed (add the repository to the Factory App installation in Settings, then
#       re-run with --assume-installed, or pass --continue-after-manual)
#
# Any other unexpected failure exits 1 (an ERR trap), never with a tool's own status.

set -Eeuo pipefail
trap 'exit 1' ERR
export LC_ALL=C
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HELPER="$SCRIPT_DIR/tfvars_repo_configs.py"
VAR_NAME="INFRA_TFVARS"
VAR_LIMIT=49152 # GitHub: one Actions variable holds at most 48 KB
GATE="factory/gate"

ORG="" REPO="" SLUG="" FACTORY_REPO="" RULESET="factory-handoff"
PROBE_URL="" RUN_BUDGET="" VERIFY_URL=""
NO_AUTO_BUILD=0 AUTO_LABEL="" AUTO_CONCURRENCY="" AUTO_MAX_ISSUES="" AUTO_MIN_PERM=""
G_LABEL=0 G_CONCURRENCY=0 G_MAX_ISSUES=0 G_MIN_PERM=0
GIVEN_FACTORY_REPO=0 GIVEN_PROBE=0 GIVEN_BUDGET=0 GIVEN_VERIFY=0
APPLY=0 CONTINUE_MANUAL=0 REPLACE=0 ASSUME_INSTALLED=0 GIVEN_DOMAINS=0
WRITTEN=0 # 1 from the moment INFRA_TFVARS was written until it has been verified
DOMAINS=()
BODY_FILE=""
TMP=""

usage() {
  cat <<'USAGE'
usage: scripts/register-repo.sh --org ORG --repo NAME --factory-app-slug SLUG \
         [--factory-repo ORG/NAME] [--ruleset NAME] [--verify-domain HOST ...] \
         [--probe-url https://HOST/PATH] [--run-budget USD] [--replace-config] \
         [--no-auto-build] [--auto-build-label NAME] [--auto-build-concurrency N] \
         [--auto-build-max-issues N] [--auto-build-min-permission LEVEL] \
         [--verify-url URL] [--assume-installed] [--continue-after-manual] [--apply]

  --org, --repo              the repository to register (same organisation as the Factory App)
  --factory-app-slug SLUG    the Factory App's slug (without [bot])
  --factory-repo ORG/NAME    the Factory repository that holds the INFRA_TFVARS variable
                             (default: 'gh repo view' of the current checkout)
  --ruleset NAME             the hand-off ruleset that must NOT match the repository
                             (default factory-handoff)
  --verify-domain HOST       host the verify phase may reach (repeatable; default pypi.org and
                             files.pythonhosted.org)
  --probe-url URL            preflight probe (default https://<first verify domain>/); its host
                             must be one of the verify domains
  --run-budget USD           run_budget_usd: a positive number of dollars (default: none)
  --no-auto-build            write auto_build = false: the manager does not watch this repository for
                             the factory-build label (the default is to watch every registered one)
  --auto-build-label NAME    auto_build_label: the issue label that asks for a build (default factory-build)
  --auto-build-concurrency N auto_build_concurrency: workers per automatic run, 1 to 64 (default 1)
  --auto-build-max-issues N  auto_build_max_issues: issues taken per cycle, 1 to 50 (default 3)
  --auto-build-min-permission LEVEL
                             auto_build_min_permission: read, triage, write, maintain or admin (default
                             write): the repository permission the user who applied the label needs.
                             Below write only for a private repository (refused, exit 3, otherwise)
  --replace-config           overwrite an existing, different entry for this repository
  --verify-url URL           also check GET URL/repos (read-only); the token comes from the
                             environment variable FACTORY_API_TOKEN, never from an argument
  --assume-installed         you have added the repository to the Factory App installation in
                             Settings: when this token cannot list the installation's
                             repositories (the normal case for a gh OAuth token), do not try the
                             API call that is known to fail and do not stop with exit 10
  --continue-after-manual    do not stop when the App must be added by hand (exit 10); the
                             message then still says that the installation is not proven
  --apply                    perform the writes. Without it: dry run, nothing is written.

An existing entry is compared on the fields you passed (--run-budget, --verify-domain,
--probe-url, --no-auto-build, --auto-build-*); the others are left as they are. A default
registration writes no auto_build key at all: watching is the default. Exit 0 also covers "different entry kept".
Exit codes: 0 ok, 1 failure, 2 usage, 3 preflight refused, 4 hand-off ruleset matches,
5 verification failed, 10 manual step needed.
USAGE
}

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() {
  local code=$1; shift
  if [ "$WRITTEN" -eq 1 ] && [ "$code" -ne 5 ]; then
    # after the write every failure is a verification failure, never "preflight refused"
    set -- "the variable $VAR_NAME was written but does not verify; fix it by hand ($*)"
    code=5
  fi
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}
step() { printf '\n== Step %s ==\n' "$*"; }

# Show a command for the log: long arguments are abbreviated.
fmt_cmd() {
  local a out=""
  for a in "$@"; do
    if [ "${#a}" -gt 100 ]; then a="<${#a} chars>"; fi
    out+="$(printf '%q' "$a") "
  done
  printf '%s' "${out% }"
}

# The ONLY way this script changes anything. do_write DESCRIPTION CMD ARGS...
# BODY_FILE (global), when non-empty, is a file passed on stdin (then cleared).
# Dry run: print, do nothing, succeed.
do_write() {
  local desc=$1; shift
  if [ "$APPLY" -ne 1 ]; then
    printf 'DRY-RUN  %s\n         would run: %s\n' "$desc" "$(fmt_cmd "$@")"
    if [ -n "$BODY_FILE" ]; then
      printf '         with the new %s text on stdin (%s bytes)\n' "$VAR_NAME" "$(wc -c <"$BODY_FILE" | tr -d ' ')"
    fi
    BODY_FILE=""
    return 0
  fi
  printf 'APPLY    %s\n         running: %s\n' "$desc" "$(fmt_cmd "$@")"
  local rc=0
  if [ -n "$BODY_FILE" ]; then
    "$@" <"$BODY_FILE" >/dev/null || rc=$?
  else
    "$@" </dev/null >/dev/null || rc=$?
  fi
  BODY_FILE=""
  return "$rc"
}

# In a dry run a stop condition is only announced; with --apply it ends the script.
stop_or_note() {
  local code=$1; shift
  if [ "$APPLY" -eq 1 ]; then
    die "$code" "$*"
  fi
  say "NOTE: --apply would stop here (exit $code): $*"
}

urlenc() { jq -rn --arg s "$1" '$s|@uri'; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
oneline() { tr -d '[:cntrl:]' | cut -c1-200; }

# ---------------------------------------------------------------- validation (no gh needed)
no_ctrl() { [[ $1 != *[[:cntrl:]]* ]]; }
valid_name_part() { [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] && no_ctrl "$1"; }
valid_repo_name() { [[ $1 =~ ^[A-Za-z0-9._-]+$ && $1 != *..* && $1 != . && $1 != *. && $1 != *.git ]] && no_ctrl "$1"; }
# A hostname: dot separated labels, a letter-initial last label (so no IP addresses), no trailing
# dot, no wildcard, 253 characters at most.
valid_host() {
  [ "${#1}" -le 253 ] || return 1
  no_ctrl "$1" || return 1
  [[ $1 =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}
valid_branch() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && $1 != *..* && $1 != */ && $1 != *.lock && $1 != *//* && ${#1} -le 200 ]] && no_ctrl "$1"
}
valid_budget() {
  [[ $1 =~ ^(0|[1-9][0-9]{0,6})(\.[0-9]{1,6})?$ ]] || return 1
  [[ $1 =~ ^0(\.0+)?$ ]] && return 1
  return 0
}
# A label name: plain characters only (it ends up in a TOML string, an API path and log fields),
# and never the ignore label or one of Factory's own factory:* status labels (the colon is not allowed anyway).
valid_label() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._/\ -]{0,49}$ ]] || return 1
  [[ $1 == "${1% }" ]] || return 1
  [ "$(lower "$1")" != "factory-ignore" ]
}
valid_count() { # VALUE MAX
  [[ $1 =~ ^[1-9][0-9]{0,2}$ ]] && [ "$1" -le "$2" ]
}
valid_token() { [[ $1 =~ ^[A-Za-z0-9._~+/=-]{1,8192}$ ]]; }

need_val() { [ "$#" -ge 2 ] || { usage >&2; die 2 "$1 needs a value"; }; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --org) need_val "$@"; ORG=$2; shift 2 ;;
    --repo) need_val "$@"; REPO=$2; shift 2 ;;
    --factory-app-slug) need_val "$@"; SLUG=$2; shift 2 ;;
    --factory-repo) need_val "$@"; FACTORY_REPO=$2; GIVEN_FACTORY_REPO=1; shift 2 ;;
    --ruleset) need_val "$@"; RULESET=$2; shift 2 ;;
    --verify-domain) need_val "$@"; DOMAINS+=("$2"); GIVEN_DOMAINS=1; shift 2 ;;
    --probe-url) need_val "$@"; PROBE_URL=$2; GIVEN_PROBE=1; shift 2 ;;
    --run-budget) need_val "$@"; RUN_BUDGET=$2; GIVEN_BUDGET=1; shift 2 ;;
    --no-auto-build) NO_AUTO_BUILD=1; shift ;;
    --auto-build-label) need_val "$@"; AUTO_LABEL=$2; G_LABEL=1; shift 2 ;;
    --auto-build-concurrency) need_val "$@"; AUTO_CONCURRENCY=$2; G_CONCURRENCY=1; shift 2 ;;
    --auto-build-max-issues) need_val "$@"; AUTO_MAX_ISSUES=$2; G_MAX_ISSUES=1; shift 2 ;;
    --auto-build-min-permission) need_val "$@"; AUTO_MIN_PERM=$2; G_MIN_PERM=1; shift 2 ;;
    --verify-url) need_val "$@"; VERIFY_URL=$2; GIVEN_VERIFY=1; shift 2 ;;
    --replace-config) REPLACE=1; shift ;;
    --assume-installed) ASSUME_INSTALLED=1; shift ;;
    --continue-after-manual) CONTINUE_MANUAL=1; shift ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done

for v in ORG REPO SLUG; do
  if [ -z "${!v}" ]; then usage >&2; die 2 "missing required option for $v"; fi
done
valid_name_part "$ORG" || die 2 "invalid --org"
valid_repo_name "$REPO" || die 2 "invalid --repo"
valid_name_part "$SLUG" || die 2 "invalid --factory-app-slug (no [bot], letters digits and dashes)"
if [ "$GIVEN_FACTORY_REPO" -eq 1 ]; then
  { [[ $FACTORY_REPO =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$ && $FACTORY_REPO != *..* ]] && no_ctrl "$FACTORY_REPO"; } \
    || die 2 "invalid --factory-repo (expected ORG/NAME)"
fi
{ [ -n "$RULESET" ] && no_ctrl "$RULESET"; } || die 2 "invalid --ruleset"
if [ "${#DOMAINS[@]}" -eq 0 ]; then
  DOMAINS=("pypi.org" "files.pythonhosted.org")
else
  CLEAN=()
  for d in "${DOMAINS[@]}"; do
    d=$(lower "$d")
    valid_host "$d" || die 2 "invalid --verify-domain (a plain host name such as pypi.org: no scheme, port, wildcard, trailing dot or quotes)"
    dup=0
    for e in ${CLEAN[@]+"${CLEAN[@]}"}; do if [ "$e" = "$d" ]; then dup=1; fi; done
    if [ "$dup" -eq 0 ]; then CLEAN+=("$d"); fi
  done
  DOMAINS=("${CLEAN[@]}")
fi
if [ "$GIVEN_PROBE" -eq 0 ]; then
  PROBE_URL="https://${DOMAINS[0]}/"
else
  { [[ $PROBE_URL =~ ^https://([a-zA-Z0-9.-]+)(/[A-Za-z0-9._~/+-]*)?$ ]] && no_ctrl "$PROBE_URL"; } \
    || die 2 "invalid --probe-url (https://HOST/PATH with letters, digits and . _ ~ / + - only; no port, user, query or fragment)"
  probe_host=$(lower "${BASH_REMATCH[1]}")
  probe_path=${BASH_REMATCH[2]}
  valid_host "$probe_host" || die 2 "invalid --probe-url host"
  found=0
  for d in "${DOMAINS[@]}"; do if [ "$d" = "$probe_host" ]; then found=1; fi; done
  [ "$found" -eq 1 ] || die 2 "the host of --probe-url ($probe_host) must be one of the --verify-domain values"
  PROBE_URL="https://$probe_host$probe_path"
fi
if [ "$GIVEN_BUDGET" -eq 1 ]; then
  valid_budget "$RUN_BUDGET" || die 2 "invalid --run-budget (a positive number of dollars such as 25 or 12.5)"
fi
if [ "$G_LABEL" -eq 1 ]; then
  valid_label "$AUTO_LABEL" || die 2 "invalid --auto-build-label (1 to 50 letters, digits, space . _ / -; no space at the edges; not factory-ignore and not a factory:* label)"
fi
if [ "$G_CONCURRENCY" -eq 1 ]; then
  valid_count "$AUTO_CONCURRENCY" 64 || die 2 "invalid --auto-build-concurrency (an integer from 1 to 64)"
fi
if [ "$G_MAX_ISSUES" -eq 1 ]; then
  valid_count "$AUTO_MAX_ISSUES" 50 || die 2 "invalid --auto-build-max-issues (an integer from 1 to 50)"
fi
if [ "$G_MIN_PERM" -eq 1 ]; then
  case "$AUTO_MIN_PERM" in
    read|triage|write|maintain|admin) ;;
    *) die 2 "invalid --auto-build-min-permission (read, triage, write, maintain or admin)" ;;
  esac
fi
if [ "$GIVEN_VERIFY" -eq 1 ]; then
  VERIFY_URL=${VERIFY_URL%/}
  if ! { [[ $VERIFY_URL =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]] \
         || [[ $VERIFY_URL =~ ^http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$ ]]; } || ! no_ctrl "$VERIFY_URL"; then
    die 2 "invalid --verify-url (https://HOST[:PORT][/PATH], or http:// for localhost only)"
  fi
  [ -n "${FACTORY_API_TOKEN:-}" ] || die 2 "--verify-url needs the manager API token in the environment variable FACTORY_API_TOKEN"
  valid_token "$FACTORY_API_TOKEN" || die 2 "FACTORY_API_TOKEN has characters a bearer token does not have (its value is not shown)"
  command -v curl >/dev/null 2>&1 || die 3 "curl not found on PATH (needed for --verify-url)"
fi

command -v gh >/dev/null 2>&1 || die 3 "gh not found on PATH"
command -v jq >/dev/null 2>&1 || die 3 "jq not found on PATH"
command -v python3 >/dev/null 2>&1 || die 3 "python3 not found on PATH (the INFRA_TFVARS editor is a Python script)"
[ -f "$HELPER" ] || die 1 "helper not found: $HELPER"

FULL="$ORG/$REPO"
TMP=$(mktemp -d) || die 1 "cannot create a temporary directory"
trap 'rm -rf -- "$TMP"' EXIT
ENTRY_FILE="$TMP/entry.toml"
RAW1="$TMP/var-first.raw"
RAW2="$TMP/var-second.raw"
RAW3="$TMP/var-after.raw"
NEWVAR="$TMP/var-new.tfvars"

if [ "$APPLY" -eq 1 ]; then say "MODE: APPLY (writes are performed)"; else say "MODE: DRY RUN (nothing is written; add --apply to perform the steps)"; fi
say "Repository: $FULL   manual-merge registration (no enrolment workflow, no ruleset; a human merges)"

# ---------------------------------------------------------------- step 0: preflight
step "0: preflight (read-only)"

hdr=$(gh api -i user 2>/dev/null | tr -d '\r') || die 3 "gh is not authenticated (run: gh auth login)"
scopes_line=$(printf '%s\n' "$hdr" | sed -n 's/^[Xx]-[Oo][Aa][Uu][Tt][Hh]-[Ss][Cc][Oo][Pp][Ee][Ss]: *//p' | head -n1)
have_scope() { case ",$(printf '%s' "$scopes_line" | tr -d ' ')," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
if [ -n "$scopes_line" ]; then
  say "token scopes: $scopes_line"
  have_scope "repo" || die 3 "token lacks the repo scope (reading the repository and writing the $VAR_NAME variable need it): gh auth refresh -s repo"
  if ! have_scope "admin:org" && ! have_scope "write:org" && ! have_scope "read:org"; then
    warn "token has no org scope; listing the organisation's App installations and rulesets may fail (gh auth refresh -s admin:org)"
  fi
else
  warn "token scopes are not reported (fine-grained token?); cannot verify them. Needed: repository Metadata (read), Variables (write) on the Factory repository, and organisation Administration (read) for installations and rulesets."
fi

if [ -z "$FACTORY_REPO" ]; then
  FACTORY_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null | tr -d '\r') \
    || die 3 "cannot tell which repository is the Factory repository ('gh repo view' of the current checkout failed); pass --factory-repo ORG/NAME"
  [[ $FACTORY_REPO =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$ && $FACTORY_REPO != *..* ]] \
    || die 3 "'gh repo view' did not return ORG/NAME; pass --factory-repo ORG/NAME"
  say "Factory repository (from the current checkout): $FACTORY_REPO"
else
  say "Factory repository: $FACTORY_REPO"
fi

fac_line=$(gh api "repos/$FACTORY_REPO" --jq '[.default_branch,.archived]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "Factory repository $FACTORY_REPO not found or not accessible"
IFS=$'\t' read -r FACTORY_BRANCH FACTORY_ARCHIVED <<<"$fac_line"
[ "$FACTORY_ARCHIVED" = "false" ] || die 3 "Factory repository $FACTORY_REPO is archived; its variable cannot be written"
valid_branch "$FACTORY_BRANCH" || die 3 "unexpected default branch name for $FACTORY_REPO"

repo_line=$(gh api "repos/$FULL" --jq '[.id,.owner.login,.archived,.default_branch,.visibility,.fork,.full_name]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "repository $FULL not found or not accessible"
IFS=$'\t' read -r REPO_ID REPO_OWNER REPO_ARCHIVED BRANCH VISIBILITY REPO_FORK REPO_CANON <<<"$repo_line"
[[ $REPO_ID =~ ^[0-9]+$ ]] || die 3 "unexpected repository id from the API"
if [ "$(lower "$REPO_OWNER")" != "$(lower "$ORG")" ]; then
  die 3 "$FULL belongs to $REPO_OWNER, not $ORG (registration is within one organisation)"
fi
# The key of the repo_configs entry and its repo = line use the name as GitHub spells it, not as typed.
[[ ${REPO_CANON:-} =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$ && $REPO_CANON != *..* ]] \
  || die 3 "unexpected repository name from the API"
if [ "$(lower "$REPO_CANON")" != "$(lower "$FULL")" ]; then
  die 3 "the API answered with $REPO_CANON for $FULL (renamed or transferred?); use the current name"
fi
if [ "$REPO_CANON" != "$FULL" ]; then
  say "note: GitHub spells the repository $REPO_CANON; that spelling is used (not $FULL)"
  FULL=$REPO_CANON
  REPO=${FULL#*/}
fi
[ "$REPO_ARCHIVED" = "false" ] || die 3 "$FULL is archived; Factory cannot push to it"
[ "$REPO_FORK" != "true" ] || die 3 "$FULL is a fork; Factory is not meant for forks (closing keywords and pull requests target the upstream)"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "null" ]; then die 3 "could not read the default branch"; fi
valid_branch "$BRANCH" || die 3 "the default branch name of $FULL has characters this script will not put into a configuration file"
BRANCH_ENC=$(urlenc "$BRANCH")
# valid_branch above admits only characters that are legal in a URL path, so the git ref is sent
# as it is (a '/' in a branch name stays a '/': %2F can make this endpoint answer 404)
gh api "repos/$FULL/git/ref/heads/$BRANCH" --jq '.object.sha' >/dev/null 2>&1 \
  || die 3 "$FULL has no commits on '$BRANCH' (empty repository): push an initial commit first"
say "repository: id $REPO_ID, default branch '$BRANCH', visibility $VISIBILITY"
# Applying a label needs only triage access; below write the manager would let people who cannot
# write to the repository start paid builds. It refuses that for a repository that is not private
# at every poll; refusing it here keeps the entry from being written at all.
case "$AUTO_MIN_PERM" in
  read|triage)
    [ "$VISIBILITY" = "private" ] || die 3 "--auto-build-min-permission $AUTO_MIN_PERM is only allowed for a private repository; $FULL is $VISIBILITY" ;;
esac

# The Factory App's installation among the organisation's installations.
FACTORY_INST="" FACTORY_APP_ID="" FACTORY_SEL=""
slug_lc=$(lower "$SLUG")
inst_lines=$(gh api --paginate "orgs/$ORG/installations" --jq '.installations[] | [.id,.app_id,.app_slug,.repository_selection]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "cannot list the installations of $ORG (needs an organisation owner and the admin:org scope)"
while IFS=$'\t' read -r i_id i_app i_slug i_sel; do
  [ -n "${i_id:-}" ] || continue
  if [ "$(lower "$i_slug")" = "$slug_lc" ]; then FACTORY_INST=$i_id; FACTORY_APP_ID=$i_app; FACTORY_SEL=$i_sel; fi
done <<<"$inst_lines"
[ -n "$FACTORY_INST" ] || die 3 "no installation of the Factory App with slug '$SLUG' in $ORG"
[[ $FACTORY_INST =~ ^[0-9]+$ && $FACTORY_APP_ID =~ ^[0-9]+$ ]] || die 3 "unexpected installation data from the API"
say "Factory App: id $FACTORY_APP_ID, installation $FACTORY_INST ($FACTORY_SEL repositories)"
SETTINGS_URL="https://github.com/organizations/$ORG/settings/installations/$FACTORY_INST"

# Manual mode is meaningless when the hand-off ruleset already holds the repository: every pull
# request into the default branch would need factory/gate, which only Factory supplies.
rs_lines=$(gh api --paginate "orgs/$ORG/rulesets" --jq '.[] | [.id,.name]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "cannot list the rulesets of $ORG (needs the admin:org scope); refusing to guess whether the hand-off ruleset holds $REPO"
RS_ID=""
while IFS=$'\t' read -r r_id r_name; do
  if [ -n "${r_id:-}" ] && [ "$r_name" = "$RULESET" ]; then RS_ID=$r_id; break; fi
done <<<"$rs_lines"
if [ -n "$RS_ID" ]; then
  [[ $RS_ID =~ ^[0-9]+$ ]] || die 3 "unexpected ruleset id from the API"
  RS_JSON=$(gh api "orgs/$ORG/rulesets/$RS_ID" 2>/dev/null) || die 3 "cannot read ruleset $RS_ID"
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$RS_JSON" || die 3 "the answer for ruleset $RS_ID is not a JSON object"
  if [ "$(jq -r '.conditions.repository_name != null' <<<"$RS_JSON")" != "true" ]; then
    warn "ruleset '$RULESET' selects repositories by something other than repository_name; only the active rules of $REPO are checked below"
  else
    in_list() { # kind (include|exclude)
      local p
      while IFS= read -r p; do
        [ -n "$p" ] || continue
        # shellcheck disable=SC2053 # $p is a ruleset repository_name pattern (fnmatch): a glob match is intended
        if [ "$p" = "~ALL" ] || [[ $REPO == $p ]]; then return 0; fi
      done < <(jq -r --arg k "$1" '.conditions.repository_name[$k][]?' <<<"$RS_JSON")
      return 1
    }
    if in_list include && ! in_list exclude; then
      die 4 "$REPO is already matched by the hand-off ruleset '$RULESET': every pull request into its default branch needs $GATE, which only Factory supplies, so human pull requests are blocked and manual mode is meaningless. Use scripts/enroll-repo.sh for it, or remove it from the ruleset first."
    fi
    say "ruleset '$RULESET' (id $RS_ID) does not match $REPO"
  fi
else
  say "ruleset '$RULESET' does not exist in $ORG; nothing to conflict with"
fi
if rules=$(gh api "repos/$FULL/rules/branches/$BRANCH_ENC" 2>/dev/null) && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$rules"; then
  gate_n=$(jq --arg g "$GATE" '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[]? | select(.context == $g)] | length' <<<"$rules") \
    || die 1 "unexpected answer for the active rules"
  if [ "$gate_n" -ge 1 ]; then
    die 4 "the active rules for '$BRANCH' of $FULL already require $GATE (some ruleset holds the repository): human pull requests are blocked, manual mode is meaningless"
  fi
  say "no active rule on '$BRANCH' requires $GATE"
else
  warn "could not read the active rules for '$BRANCH' of $FULL; only the named ruleset was checked"
fi

# The repo_configs entry (validated values only; see the validators above).
{
  printf 'repo            = "%s"\n' "$FULL"
  printf 'base_branch     = "%s"\n' "$BRANCH"
  printf 'installation_id = %s\n' "$FACTORY_INST"
  printf 'abandon_after   = "7d"\n'
  if [ -n "$RUN_BUDGET" ]; then printf 'run_budget_usd  = %s\n' "$RUN_BUDGET"; fi
  printf 'auto_resume_after_restart = "once"\n'
  # Watching every registered repository is the default, so a default registration writes none of these.
  if [ "$NO_AUTO_BUILD" -eq 1 ]; then printf 'auto_build      = false\n'; fi
  if [ "$G_LABEL" -eq 1 ]; then printf 'auto_build_label = "%s"\n' "$AUTO_LABEL"; fi
  if [ "$G_MIN_PERM" -eq 1 ]; then printf 'auto_build_min_permission = "%s"\n' "$AUTO_MIN_PERM"; fi
  if [ "$G_CONCURRENCY" -eq 1 ]; then printf 'auto_build_concurrency = %s\n' "$AUTO_CONCURRENCY"; fi
  if [ "$G_MAX_ISSUES" -eq 1 ]; then printf 'auto_build_max_issues = %s\n' "$AUTO_MAX_ISSUES"; fi
  printf '\n[worker]\nuid = 10002\ngid = 10010\n'
  printf '\n[sandbox]\nverify_domains = ['
  sep=""
  for d in "${DOMAINS[@]}"; do printf '%s"%s"' "$sep" "$d"; sep=", "; done
  printf ']\n'
  printf '\n[preflight]\nprobe_url = "%s"\n' "$PROBE_URL"
} >"$ENTRY_FILE"
# shellcheck disable=SC2016 # fixed strings: '${' is the text to find
if grep -qF -e '<<' -e '${' -e '%{' "$ENTRY_FILE"; then die 1 "the rendered entry contains a template or heredoc sequence; refusing"; fi

# INFRA_TFVARS: read once, plan the edit. A failed read is an error, never "empty".
read_var() { # outfile
  local rc=0
  gh variable list -R "$FACTORY_REPO" --json name,value --jq '.[]|select(.name=="INFRA_TFVARS")|.value' >"$1" 2>"$TMP/gh.err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    die 3 "cannot read the repository variables of $FACTORY_REPO (gh said: $(head -n1 "$TMP/gh.err" | oneline)); the repo scope and access to that repository are needed"
  fi
  [ -s "$1" ] || die 3 "the variable $VAR_NAME does not exist in $FACTORY_REPO (or is empty); create it first (infra/README.md)"
}
PLAN_JSON="" STATUS="" DIFF="" SIZE=""
# Fields the operator did not pass are not compared with an existing entry (the rendered values
# for them are defaults, not wishes). The check of a text this script wrote itself is strict.
IGNORE=() NOT_COMPARED=""
if [ "$GIVEN_BUDGET" -eq 0 ]; then IGNORE+=(--ignore-key run_budget_usd); NOT_COMPARED+=" run_budget_usd"; fi
if [ "$GIVEN_DOMAINS" -eq 0 ]; then IGNORE+=(--ignore-key verify_domains); NOT_COMPARED+=" verify_domains"; fi
if [ "$GIVEN_PROBE" -eq 0 ]; then IGNORE+=(--ignore-key probe_url); NOT_COMPARED+=" probe_url"; fi
if [ "$NO_AUTO_BUILD" -eq 0 ] && [ "$REPLACE" -eq 0 ]; then IGNORE+=(--ignore-key auto_build); NOT_COMPARED+=" auto_build"; fi
if [ "$G_LABEL" -eq 0 ] && [ "$REPLACE" -eq 0 ]; then IGNORE+=(--ignore-key auto_build_label); NOT_COMPARED+=" auto_build_label"; fi
if [ "$G_MIN_PERM" -eq 0 ] && [ "$REPLACE" -eq 0 ]; then IGNORE+=(--ignore-key auto_build_min_permission); NOT_COMPARED+=" auto_build_min_permission"; fi
if [ "$G_CONCURRENCY" -eq 0 ] && [ "$REPLACE" -eq 0 ]; then IGNORE+=(--ignore-key auto_build_concurrency); NOT_COMPARED+=" auto_build_concurrency"; fi
if [ "$G_MAX_ISSUES" -eq 0 ] && [ "$REPLACE" -eq 0 ]; then IGNORE+=(--ignore-key auto_build_max_issues); NOT_COMPARED+=" auto_build_max_issues"; fi
plan_edit() { # raw out [strict]
  local rc=0 replace=() ignore=()
  if [ "$REPLACE" -eq 1 ]; then replace=(--replace); fi
  if [ "${3:-}" != "strict" ]; then ignore=(${IGNORE[@]+"${IGNORE[@]}"}); fi
  PLAN_JSON=$(python3 "$HELPER" plan --raw "$1" --key "$FULL" --entry "$ENTRY_FILE" --out "$2" ${replace[@]+"${replace[@]}"} ${ignore[@]+"${ignore[@]}"} 2>"$TMP/helper.err") || rc=$?
  if [ "$rc" -eq 3 ]; then
    die 3 "$VAR_NAME in $FACTORY_REPO: $(head -n1 "$TMP/helper.err" | oneline) (edit it by hand, infra/README.md)"
  elif [ "$rc" -ne 0 ]; then
    die 1 "the $VAR_NAME editor failed (exit $rc): $(head -n1 "$TMP/helper.err" | oneline)"
  fi
  STATUS=$(jq -r '.status' <<<"$PLAN_JSON") || die 1 "unexpected answer from the editor"
  DIFF=$(jq -r '.diff' <<<"$PLAN_JSON") || die 1 "unexpected answer from the editor"
  SIZE=$(jq -r '.size' <<<"$PLAN_JSON") || die 1 "unexpected answer from the editor"
  [[ $SIZE =~ ^[0-9]+$ ]] || die 1 "unexpected answer from the editor"
}
read_var "$RAW1"
plan_edit "$RAW1" "$NEWVAR"
say "$VAR_NAME: read from $FACTORY_REPO; repo_configs map found; entry for $FULL: $STATUS"
case "$STATUS" in
  inserted|replaced)
    if [ "$SIZE" -gt "$VAR_LIMIT" ]; then
      die 3 "the new $VAR_NAME would be $SIZE bytes; a GitHub variable holds at most $VAR_LIMIT (48 KB). Move inputs out of it first."
    fi
    # The text to be written is read back HERE, before any write: it must show this entry (and
    # the editor itself has already re-parsed it and compared every other entry). A text that
    # does not verify is never written.
    keep_status=$STATUS keep_diff=$DIFF keep_size=$SIZE
    plan_edit "$NEWVAR" "$TMP/var-precheck.tfvars" strict
    [ "$STATUS" = "unchanged" ] || die 3 "the edited $VAR_NAME would not show the entry for $FULL when read back; nothing was written"
    STATUS=$keep_status DIFF=$keep_diff SIZE=$keep_size ;;
esac

# ---------------------------------------------------------------- step 1: can the Factory App reach the repository?
step "1: the Factory App must be installed on $FULL"
MANUAL="" ASSUMED=0
if [ "$FACTORY_SEL" = "all" ]; then
  say "ok: the Factory App is installed on all repositories of $ORG"
elif listed=$(gh api --paginate "user/installations/$FACTORY_INST/repositories" --jq '.repositories[].id' 2>/dev/null); then
  if grep -qxF "$REPO_ID" <<<"$listed"; then
    say "ok: the Factory App installation lists $FULL"
  else
    say "the Factory App installation does not list $FULL yet"
    MANUAL=1
  fi
elif [ "$ASSUME_INSTALLED" -eq 1 ]; then
  say "note: this token cannot list the repositories of the Factory App installation, so reach is NOT proven."
  say "--assume-installed: taking your word that $FULL is added to the installation ($SETTINGS_URL); no API call is made for it"
  ASSUMED=1
else
  say "note: this token cannot list the repositories of the Factory App installation (normal for a gh OAuth token), so reach is not proven; the add below is idempotent"
  say "      If you have already added $FULL in Settings, re-run with --assume-installed."
  MANUAL=1
fi
if [ -n "$MANUAL" ]; then
  if do_write "add $FULL to the Factory App installation" gh api -X PUT "user/installations/$FACTORY_INST/repositories/$REPO_ID" --silent; then
    if [ "$APPLY" -eq 0 ]; then
      say "NOTE: GitHub refuses this call for gh OAuth tokens (403); --apply would then stop with exit 10 and print: open $SETTINGS_URL , under 'Repository access' add $FULL, Save"
    fi
  else
    warn "GitHub refused the API call (403 is the normal answer for gh OAuth tokens)"
    say "MANUAL STEP NEEDED:"
    say "  - open $SETTINGS_URL , under 'Repository access' add $FULL, Save"
    if [ "$CONTINUE_MANUAL" -eq 1 ]; then
      say "--continue-after-manual: continuing; reach is NOT proven, make sure the installation is done before the first run"
      MANUAL="" ASSUMED=1
    else
      stop_or_note 10 "manual step needed: add the repository to the Factory App installation as listed above, then re-run this script"
    fi
  fi
fi

# ---------------------------------------------------------------- step 2: the repo_configs entry
step "2: the repo_configs entry for $FULL"
say "  (installation_id $FACTORY_INST and base_branch '$BRANCH' are read from GitHub, not assumed)"
sed 's/^/  | /' "$ENTRY_FILE"

# ---------------------------------------------------------------- step 3: INFRA_TFVARS
step "3: $VAR_NAME in $FACTORY_REPO"
CHANGED=0
case "$STATUS" in
  unchanged)
    say "ok: repo_configs already has an identical entry for $FULL; nothing to write"
    if [ -n "$NOT_COMPARED" ]; then
      say "    (compared on the fields you passed; not compared, because the option was not given:$NOT_COMPARED)"
    fi ;;
  differs)
    say "NOTE: repo_configs already has an entry for $FULL with DIFFERENT content. It is NOT overwritten."
    say "Differences (this entry only):"
    printf '%s\n' "$DIFF"
    say "Keep it as it is (exit 0, nothing written), or re-run with --replace-config to replace it with the entry above." ;;
  inserted|replaced)
    say "Change to the repo_configs region of $VAR_NAME (nothing else is shown or changed):"
    printf '%s\n' "$DIFF"
    # Lost-update guard: the variable must be exactly what the diff was computed from.
    if [ "$APPLY" -eq 1 ]; then
      read_var "$RAW2"
      cmp -s "$RAW1" "$RAW2" || die 1 "$VAR_NAME in $FACTORY_REPO changed since it was read (someone else edited it); nothing was written. Re-run to plan against the new value."
      say "verified: $VAR_NAME is unchanged since the first read"
    fi
    chmod 600 "$NEWVAR"
    BODY_FILE="$NEWVAR"
    do_write "set $VAR_NAME in $FACTORY_REPO ($STATUS entry $FULL, $SIZE bytes)" gh variable set "$VAR_NAME" -R "$FACTORY_REPO" \
      || die 1 "could not write the variable $VAR_NAME (the repo scope and write access to $FACTORY_REPO are needed)"
    CHANGED=1
    if [ "$APPLY" -eq 1 ]; then
      WRITTEN=1 # from here on every failure is exit 5, with "fix it by hand"
      read_var "$RAW3"
      plan_edit "$RAW3" "$TMP/var-check.tfvars" strict
      [ "$STATUS" = "unchanged" ] || die 5 "verification failed: $VAR_NAME in $FACTORY_REPO does not show the entry for $FULL after the write; fix it by hand"
      WRITTEN=0
      say "verified: $VAR_NAME now has the entry for $FULL"
    fi ;;
esac

# ---------------------------------------------------------------- step 4: what remains
step "4: remaining steps (this script never does them)"
if [ "$CHANGED" -eq 1 ]; then
  say "$VAR_NAME is changed (or would be), but NOTHING is rolled out until the Terraform of the reference deployment is applied with the new value."
  say "A repo_configs change restarts the manager pod, so:"
  cat <<'STEPS'

  (a) Drain every active run (docs/operations.md, "Drain, pause, and what each leaves behind"):
        GET  <<MANAGER_URL>>/repos                                              entries whose "supervisor" is not null are active
        GET  <<MANAGER_URL>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>/status         for each active run
        POST <<MANAGER_URL>>/repos/<<ORG>>/<<REPO>>/runs/<<RUN>>/drain          for each active run
        GET  <<MANAGER_URL>>/repos                                              repeat until every "supervisor" is null
STEPS
  printf '\n  (b) Apply the Terraform of infra/ with the new %s value (infra/README.md, "Safe deployments"),\n' "$VAR_NAME"
  printf '      from the checkout of %s on %s:\n' "$FACTORY_REPO" "$FACTORY_BRANCH"
  printf '        terraform -chdir=infra plan\n'
  printf '        terraform -chdir=infra apply\n'
  say "      Read the plan (a new ConfigMap, an updated Deployment) before you apply it, and wait for the"
  say "      rollout of the new pod."
  printf '\n  (c) Verify: GET <<MANAGER_URL>>/repos lists "%s" with no "error" field.\n' "$FULL"
  say "      Nothing else is needed to start work: the manager now watches $FULL, and an issue labelled"
  say "      '${AUTO_LABEL:-factory-build}' by someone with write access is planned and built by itself"
  if [ "$NO_AUTO_BUILD" -eq 1 ]; then
    say "      (NOT for this entry: it says auto_build = false, so use the manager API's plan and run by hand.)"
  fi
  say "      (--verify-url URL with FACTORY_API_TOKEN does this GET for you.)"
elif [ "$STATUS" = "differs" ]; then
  say "No change was made, so there is nothing to roll out."
else
  say "No change to $VAR_NAME was needed."
fi

# ---------------------------------------------------------------- step 5: optional read-only check of the manager
VERIFY_RESULT=""
if [ -n "$VERIFY_URL" ]; then
  step "5: GET $VERIFY_URL/repos (read-only)"
  code=$(printf 'header = "Authorization: Bearer %s"\n' "$FACTORY_API_TOKEN" \
    | curl --config - --silent --show-error --globoff --max-time 30 --output "$TMP/repos.json" --write-out '%{http_code}' --url "$VERIFY_URL/repos" 2>"$TMP/curl.err") \
    || die 1 "GET $VERIFY_URL/repos failed ($(head -n1 "$TMP/curl.err" | oneline))"
  [ "$code" = "200" ] || die 1 "GET $VERIFY_URL/repos answered HTTP $code"
  jq -e '.repos | type == "array"' >/dev/null 2>&1 <"$TMP/repos.json" || die 1 "the answer of GET $VERIFY_URL/repos is not a repository list"
  VERIFY_RESULT=$(jq -r --arg r "$FULL" '[.repos[] | select((.repo // "" | ascii_downcase) == ($r | ascii_downcase))] | if length == 0 then "absent" elif (.[0] | has("error")) then "error" else "listed" end' <"$TMP/repos.json") \
    || die 1 "cannot read the answer of GET $VERIFY_URL/repos"
  case "$VERIFY_RESULT" in
    listed) say "listed: the manager lists $FULL with no error field" ;;
    error) die 5 "listed with error: the manager lists $FULL but reports an error for its configuration (check the entry; infra/README.md)" ;;
    absent)
      if [ "$CHANGED" -eq 1 ]; then
        say "absent: the manager does not list $FULL yet, as expected before steps (a) and (b)"
      else
        die 5 "absent: the entry is in $VAR_NAME but the manager does not list $FULL; the Terraform has not been applied since it was added (steps (a) and (b))"
      fi ;;
  esac
fi

if [ "$CHANGED" -eq 0 ] && [ -z "$MANUAL" ] && [ "$STATUS" = "unchanged" ]; then
  say ""
  if [ "$ASSUMED" -eq 1 ]; then
    say "NOTE: the App installation was ASSUMED (--assume-installed), not checked."
  fi
  if [ "$VERIFY_RESULT" = "listed" ]; then
    say "NO-OP: $FULL is registered in manual mode and the manager lists it; nothing to do."
  else
    say "NO-OP: $FULL is registered in manual mode (App installed, entry present and identical); nothing to do."
    say "To see whether the manager runs with it: GET <<MANAGER_URL>>/repos, or re-run with --verify-url."
  fi
fi
if [ "$APPLY" -eq 0 ]; then
  say ""
  say "DRY RUN complete: nothing was written. Re-run with --apply to perform the steps above."
fi
exit 0
