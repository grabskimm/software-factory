#!/usr/bin/env bash
# Enrol ONE new repository (same organisation) into the Factory hand-off.
#
# Automates exactly the per-repository steps of docs/handoff.md "Operator setup",
# idempotently, for an organisation owner's gh login.
# See docs/enrolling-repositories.md. DEFAULT IS A DRY RUN: nothing is written without --apply.
#
# Rules this script keeps (do not "improve" them away):
#   - the enrolment workflow is on the default branch BEFORE the ruleset is touched;
#   - one ruleset per group of repositories that share CI check names;
#   - no bypass actors, ever; no branch deletion; no force push; no `gh pr merge --admin`;
#   - the Factory App is never granted `workflows`; no secret value is ever read or printed.
#
# Exit codes:
#    0  done (or dry run finished)
#    1  a gh call or an unexpected condition failed
#    2  usage error
#    3  preflight refused (auth/scopes, repo missing/archived/foreign, App/secret/ruleset missing)
#    4  ruleset step refused (check names differ, repo excluded, unsupported conditions,
#       repo already matched by a ruleset before the workflow exists). All of these are decided
#       in the read-only preflight, so with --apply they come before any write.
#    5  final verification failed (rules endpoint does not show factory/gate with the App id, or
#       it requires a check set other than the confirmed one)
#   10  manual step needed (install an App on the repository in Settings, then re-run)
#   11  merge the enrolment PR (URL printed), then re-run
#   12  CI check names must be confirmed with --ci-check (or --no-ci); with --new-ruleset a name
#       never seen on the default branch also needs --allow-unseen-check
#
# Any other unexpected failure exits 1 (an ERR trap), never with a tool's own status.

set -Eeuo pipefail
trap 'exit 1' ERR
export LC_ALL=C
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
HANDOFF_DIR="$SCRIPT_DIR/../handoff"
WORKFLOW_PATH=".github/workflows/factory-automerge.yml"
SECRET_NAME="AUTOMERGE_APP_PRIVATE_KEY"
ENROLL_BRANCH="factory/enroll"
GATE="factory/gate"
RETRY_SLEEP="${ENROLL_RETRY_SLEEP:-3}"

ORG="" REPO="" SLUG="" CLIENT_ID=""
RULESET="" RULESET_GIVEN=0 NEW_RULESET=0
WORKFLOW_MODE="pr"
APPLY=0 CONTINUE_MANUAL=0 NO_CI=0 ALLOW_UNSEEN=0
CI_CHECKS=()
BODY=""
LAST_OUT=""

usage() {
  cat <<'USAGE'
usage: scripts/enroll-repo.sh --org ORG --repo NAME --factory-app-slug SLUG \
         --automerge-client-id Iv... [--ci-check NAME ...] [--no-ci] \
         [--ruleset NAME] [--new-ruleset] [--workflow-mode pr|direct] \
         [--allow-unseen-check] [--continue-after-manual] [--apply]

  --org, --repo              the repository to enrol (same organisation as the Apps)
  --factory-app-slug SLUG    the Factory App's slug (without [bot])
  --automerge-client-id ID   the merge-enrolment App's Client ID (Iv...)
  --ci-check NAME            exact CI check name as the job reports it (repeatable). Required
                             with --apply; without it the discovered names are only printed.
  --no-ci                    the repository has no CI checks (only factory/gate is required)
  --ruleset NAME             ruleset to extend (default factory-handoff; with --new-ruleset
                             default factory-handoff-<repo>)
  --new-ruleset              create the ruleset from handoff/ruleset.json when NAME does not exist
  --workflow-mode pr|direct  pr (default): branch factory/enroll + pull request;
                             direct: commit to the default branch (only while the repository is
                             not yet matched by the ruleset)
  --allow-unseen-check       with --new-ruleset: accept a --ci-check name that was not seen on the
                             default branch's head (checks that only run on pull requests). A
                             wrong name would block every pull request in the repository.
  --continue-after-manual    do not stop when an App must be installed by hand (exit 10)
  --apply                    perform the writes. Without it: dry run, nothing is written.

Exit codes: 0 ok, 1 failure, 2 usage, 3 preflight refused, 4 ruleset refused, 5 verify failed,
10 manual step needed, 11 merge the PR then re-run, 12 confirm CI checks.
USAGE
}

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { local code=$1; shift; printf 'ERROR: %s\n' "$*" >&2; exit "$code"; }
step() { printf '\n== Step %s ==\n' "$*"; }

# Show a command for the log: long arguments (file contents) are abbreviated.
fmt_cmd() {
  local a out=""
  for a in "$@"; do
    if [ "${#a}" -gt 100 ]; then a="<${#a} chars>"; fi
    out+="$(printf '%q' "$a") "
  done
  printf '%s' "${out% }"
}

# The ONLY way this script changes anything. do_write DESCRIPTION CMD ARGS...
# BODY (global), when non-empty, is a JSON document passed on stdin. Output of the command
# is left in LAST_OUT. Dry run: print, do nothing, succeed.
do_write() {
  local desc=$1; shift
  if [ "$APPLY" -ne 1 ]; then
    printf 'DRY-RUN  %s\n         would run: %s\n' "$desc" "$(fmt_cmd "$@")"
    if [ -n "$BODY" ]; then
      printf '         with this JSON on stdin:\n'
      printf '%s' "$BODY" | jq . | sed 's/^/           /'
    fi
    LAST_OUT=""
    BODY=""
    return 0
  fi
  printf 'APPLY    %s\n         running: %s\n' "$desc" "$(fmt_cmd "$@")"
  local rc=0
  if [ -n "$BODY" ]; then
    LAST_OUT=$(printf '%s' "$BODY" | "$@") || rc=$?
  else
    LAST_OUT=$("$@" </dev/null) || rc=$?
  fi
  BODY=""
  if [ "$rc" -ne 0 ]; then
    return "$rc"
  fi
  return 0
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

# ---------------------------------------------------------------- arguments
need_val() { [ "$#" -ge 2 ] || { usage >&2; die 2 "$1 needs a value"; }; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --org) need_val "$@"; ORG=$2; shift 2 ;;
    --repo) need_val "$@"; REPO=$2; shift 2 ;;
    --factory-app-slug) need_val "$@"; SLUG=$2; shift 2 ;;
    --automerge-client-id) need_val "$@"; CLIENT_ID=$2; shift 2 ;;
    --ci-check) need_val "$@"; CI_CHECKS+=("$2"); shift 2 ;;
    --no-ci) NO_CI=1; shift ;;
    --ruleset) need_val "$@"; RULESET=$2; RULESET_GIVEN=1; shift 2 ;;
    --new-ruleset) NEW_RULESET=1; shift ;;
    --workflow-mode) need_val "$@"; WORKFLOW_MODE=$2; shift 2 ;;
    --continue-after-manual) CONTINUE_MANUAL=1; shift ;;
    --allow-unseen-check) ALLOW_UNSEEN=1; shift ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done

for v in ORG REPO SLUG CLIENT_ID; do
  if [ -z "${!v}" ]; then usage >&2; die 2 "missing required option for $v"; fi
done
[[ $ORG =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die 2 "invalid --org"
[[ $REPO =~ ^[A-Za-z0-9._-]+$ && $REPO != *..* && $REPO != . ]] || die 2 "invalid --repo"
[[ $SLUG =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die 2 "invalid --factory-app-slug (no [bot], letters digits and dashes)"
[[ $CLIENT_ID =~ ^Iv[0-9A-Za-z._]+$ ]] || die 2 "invalid --automerge-client-id (expected Iv...)"
case "$WORKFLOW_MODE" in pr|direct) ;; *) die 2 "--workflow-mode must be pr or direct" ;; esac
if [ "$RULESET_GIVEN" -eq 0 ]; then
  if [ "$NEW_RULESET" -eq 1 ]; then RULESET="factory-handoff-$REPO"; else RULESET="factory-handoff"; fi
fi
[[ $RULESET != *[[:cntrl:]]* && -n $RULESET ]] || die 2 "invalid --ruleset"
if [ "$NO_CI" -eq 1 ] && [ "${#CI_CHECKS[@]}" -gt 0 ]; then die 2 "--no-ci and --ci-check exclude each other"; fi
if [ "${#CI_CHECKS[@]}" -gt 0 ]; then
  for c in "${CI_CHECKS[@]}"; do
    [[ -n $c && $c != *[[:cntrl:]]* && $c != "$GATE" ]] || die 2 "invalid --ci-check value (empty, control characters, or $GATE)"
  done
fi

command -v gh >/dev/null 2>&1 || die 3 "gh not found on PATH"
command -v jq >/dev/null 2>&1 || die 3 "jq not found on PATH"
command -v base64 >/dev/null 2>&1 || die 3 "base64 not found on PATH"

# Rendered once, before any gh call. `--` keeps a value that starts with '-' from being read as a jq option.
if [ "${#CI_CHECKS[@]}" -gt 0 ]; then
  CHECKS_JSON=$(jq -n '$ARGS.positional' --args -- "${CI_CHECKS[@]}") || die 1 "could not render the --ci-check list"
else
  CHECKS_JSON='[]'
fi
MY_CHECKS=$(jq -r 'unique | .[]' <<<"$CHECKS_JSON") || die 1 "could not render the --ci-check list"

FULL="$ORG/$REPO"
if [ "$APPLY" -eq 1 ]; then say "MODE: APPLY (writes are performed)"; else say "MODE: DRY RUN (nothing is written; add --apply to perform the steps)"; fi
say "Repository: $FULL   ruleset: $RULESET   workflow mode: $WORKFLOW_MODE"

# ---------------------------------------------------------------- render the workflow (no gh needed)
render_workflow() {
  local tpl="$HANDOFF_DIR/factory-automerge.yml" content
  [ -f "$tpl" ] || die 1 "template not found: $tpl"
  content=$(cat "$tpl"; printf x); content=${content%x}
  content=${content//'<<GITHUB_APP_SLUG>>'/$SLUG}
  content=${content//'<<AUTOMERGE_APP_CLIENT_ID>>'/$CLIENT_ID}
  case "$content" in
    *'<<'*) die 1 "the rendered workflow still contains '<<' (an unreplaced placeholder); refusing" ;;
  esac
  printf '%s' "$content"
}
WORKFLOW_CONTENT=$(render_workflow)
WORKFLOW_B64=$(printf '%s\n' "$WORKFLOW_CONTENT" | base64 | tr -d '\n')

# ---------------------------------------------------------------- step 0: preflight
step "0: preflight (read-only)"

hdr=$(gh api -i user 2>/dev/null | tr -d '\r') || die 3 "gh is not authenticated (run: gh auth login)"
scopes_line=$(printf '%s\n' "$hdr" | sed -n 's/^[Xx]-[Oo][Aa][Uu][Tt][Hh]-[Ss][Cc][Oo][Pp][Ee][Ss]: *//p' | head -n1)
have_scope() { case ",$(printf '%s' "$scopes_line" | tr -d ' ')," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
if [ -n "$scopes_line" ]; then
  say "token scopes: $scopes_line"
  have_scope "admin:org" || die 3 "token lacks the admin:org scope (org secrets and org rulesets need it): gh auth refresh -s admin:org"
  have_scope "repo" || die 3 "token lacks the repo scope: gh auth refresh -s repo"
  SCOPES_KNOWN=1
else
  warn "token scopes are not reported (fine-grained token?); cannot verify them. Org administration (write), Administration, Contents, Pull requests and Workflows (write) are needed."
  SCOPES_KNOWN=0
fi

if role=$(gh api "user/memberships/orgs/$ORG" --jq '"\(.role) \(.state)"' 2>/dev/null); then
  [ "$role" = "admin active" ] || die 3 "the gh login is not an active owner of $ORG (membership: $role)"
  say "org membership: owner"
else
  warn "could not read the membership in $ORG; continuing, the writes will fail if the login is not an owner"
fi

repo_line=$(gh api "repos/$FULL" --jq '[.id,.full_name,.owner.login,.archived,.default_branch,.visibility,.allow_squash_merge,.allow_auto_merge,.fork]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "repository $FULL not found or not accessible"
IFS=$'\t' read -r REPO_ID _ REPO_OWNER REPO_ARCHIVED BRANCH VISIBILITY ALLOW_SQUASH ALLOW_AUTO REPO_FORK <<<"$repo_line"
[[ $REPO_ID =~ ^[0-9]+$ ]] || die 3 "unexpected repository id from the API"
if [ "$(printf '%s' "$REPO_OWNER" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$ORG" | tr '[:upper:]' '[:lower:]')" ]; then
  die 3 "$FULL belongs to $REPO_OWNER, not $ORG (enrolment is within one organisation)"
fi
[ "$REPO_ARCHIVED" = "false" ] || die 3 "$FULL is archived; nothing can be written to it"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "null" ]; then die 3 "could not read the default branch"; fi
[ "$REPO_FORK" != "true" ] || die 3 "$FULL is a fork; its Actions workflows do not run until enabled by hand and the Factory hand-off is not meant for forks"
BRANCH_ENC=$(urlenc "$BRANCH")
gh api "repos/$FULL/git/ref/heads/$BRANCH_ENC" --jq '.object.sha' >/dev/null 2>&1 \
  || die 3 "$FULL has no commits on '$BRANCH' (empty repository): push an initial commit first"
if actions_enabled=$(gh api "repos/$FULL/actions/permissions" --jq '.enabled' 2>/dev/null); then
  [ "$actions_enabled" != "false" ] || die 3 "GitHub Actions is disabled for $FULL; the enrolment workflow (and so $GATE) would never run. Enable it in the repository settings."
else
  warn "could not read whether GitHub Actions is enabled for $FULL; it must be, or $GATE is never posted"
fi
say "repository: id $REPO_ID, default branch '$BRANCH', visibility $VISIBILITY"

# Apps among the organisation's installations.
FACTORY_INST="" FACTORY_APP_ID="" FACTORY_SEL="" MERGE_INST="" MERGE_SEL=""
slug_lc=$(printf '%s' "$SLUG" | tr '[:upper:]' '[:lower:]')
inst_lines=$(gh api --paginate "orgs/$ORG/installations" --jq '.installations[] | [.id,.app_id,.app_slug,(.client_id // "-"),.repository_selection]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "cannot list the installations of $ORG (needs the admin:org scope and an owner)"
while IFS=$'\t' read -r i_id i_app i_slug i_client i_sel; do
  [ -n "${i_id:-}" ] || continue
  if [ "$(printf '%s' "$i_slug" | tr '[:upper:]' '[:lower:]')" = "$slug_lc" ]; then
    FACTORY_INST=$i_id; FACTORY_APP_ID=$i_app; FACTORY_SEL=$i_sel
  fi
  if [ "$i_client" = "$CLIENT_ID" ]; then
    MERGE_INST=$i_id; MERGE_SEL=$i_sel
  fi
done <<<"$inst_lines"
[ -n "$FACTORY_INST" ] || die 3 "no installation of the Factory App with slug '$SLUG' in $ORG"
[ -n "$MERGE_INST" ] || die 3 "no installation of the merge-enrolment App with client id '$CLIENT_ID' in $ORG"
[[ $FACTORY_APP_ID =~ ^[0-9]+$ && $FACTORY_INST =~ ^[0-9]+$ && $MERGE_INST =~ ^[0-9]+$ ]] || die 3 "unexpected installation data from the API"
[ "$FACTORY_INST" != "$MERGE_INST" ] || die 3 "the Factory App and the merge-enrolment App must be two different Apps"
say "Factory App: id $FACTORY_APP_ID, installation $FACTORY_INST ($FACTORY_SEL repositories)"
say "merge-enrolment App: installation $MERGE_INST ($MERGE_SEL repositories)"

# Ruleset.
RS_ID="" RS_JSON=""
rs_lines=$(gh api --paginate "orgs/$ORG/rulesets" --jq '.[] | [.id,.name]|map(tostring)|@tsv' 2>/dev/null) \
  || die 3 "cannot list the rulesets of $ORG (needs the admin:org scope)"
while IFS=$'\t' read -r r_id r_name; do
  if [ -n "${r_id:-}" ] && [ "$r_name" = "$RULESET" ]; then RS_ID=$r_id; break; fi
done <<<"$rs_lines"
if [ -n "$RS_ID" ]; then
  [[ $RS_ID =~ ^[0-9]+$ ]] || die 3 "unexpected ruleset id from the API"
  RS_JSON=$(gh api "orgs/$ORG/rulesets/$RS_ID") || die 3 "cannot read ruleset $RS_ID"
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$RS_JSON" || die 3 "the answer for ruleset $RS_ID is not a JSON object"
  say "ruleset '$RULESET': id $RS_ID, enforcement $(jq -r '.enforcement' <<<"$RS_JSON")"
elif [ "$NEW_RULESET" -eq 1 ]; then
  say "ruleset '$RULESET' does not exist; it will be created (--new-ruleset)"
else
  die 3 "ruleset '$RULESET' does not exist in $ORG; name an existing one with --ruleset, or use --new-ruleset"
fi

# The organisation secret (metadata only; the value is never read). Decided here, before any write.
SEC_ADD=0 SEC_NOTE=""
sec_rc=0
sec_out=$(gh api "orgs/$ORG/actions/secrets/$SECRET_NAME" --jq '.visibility' 2>&1) || sec_rc=$?
if [ "$sec_rc" -ne 0 ]; then
  sec_msg=$(printf '%s' "$sec_out" | head -n1 | tr -d '[:cntrl:]' | cut -c1-200)
  die 3 "cannot read the organisation secret $SECRET_NAME (gh said: ${sec_msg:-nothing}); create it first if it does not exist (docs/handoff.md, Operator setup 1)"
fi
case "$sec_out" in
  all)
    SEC_NOTE="$SECRET_NAME is available to all repositories" ;;
  private)
    case "$VISIBILITY" in
      private|internal) SEC_NOTE="$SECRET_NAME is available to all private repositories, which includes $FULL" ;;
      *) die 3 "$SECRET_NAME is only available to private repositories and $FULL is $VISIBILITY; change the visibility by hand, deliberately" ;;
    esac ;;
  selected)
    sec_repos=$(gh api --paginate "orgs/$ORG/actions/secrets/$SECRET_NAME/repositories" --jq '.repositories[].id' 2>/dev/null) \
      || die 1 "cannot list the selected repositories of $SECRET_NAME"
    if grep -qxF "$REPO_ID" <<<"$sec_repos"; then
      SEC_NOTE="$FULL is already among the selected repositories of $SECRET_NAME"
    else
      SEC_ADD=1
    fi ;;
  *)
    die 3 "$SECRET_NAME has visibility '$sec_out'; this script only extends 'selected' (change the visibility by hand, deliberately)" ;;
esac

# CI check names (step 5 needs them; discovered here so that --apply refuses before any write).
discover_checks() {
  {
    gh api "repos/$FULL/commits/$BRANCH_ENC/check-runs?per_page=100" --jq '.check_runs[]?.name' 2>/dev/null || true
    gh api "repos/$FULL/commits/$BRANCH_ENC/status" --jq '.statuses[]?.context' 2>/dev/null || true
  } | grep -vxF "$GATE" | sort -u || true
}
DISCOVERED=$(discover_checks)
CI_CONFIRMED=1
UNSEEN=()
if [ "$NO_CI" -eq 0 ] && [ "${#CI_CHECKS[@]}" -eq 0 ]; then
  CI_CONFIRMED=0
  say "CI checks seen on the latest '$BRANCH' commit:"
  if [ -n "$DISCOVERED" ]; then printf '%s\n' "$DISCOVERED" | sed 's/^/  - /'; else say "  (none)"; fi
  say "These names are NOT used until you confirm them: re-run with one --ci-check NAME per check (exact"
  say "job name after matrix expansion, see docs/handoff.md), or --no-ci if the repository has no CI."
  say "A wrong name blocks every pull request in the repository."
  stop_or_note 12 "CI check names not confirmed (--ci-check / --no-ci)"
else
  if [ "$NO_CI" -eq 1 ]; then say "CI checks: none (--no-ci); only $GATE will be required"; else
    say "CI checks (confirmed by --ci-check): $(printf '%s, ' "${CI_CHECKS[@]}" | sed 's/, $//')"
    for c in "${CI_CHECKS[@]}"; do
      if ! grep -qxF -- "$c" <<<"$DISCOVERED"; then
        warn "'$c' was not seen on the latest '$BRANCH' commit (fine for checks that only run on pull requests; a typo blocks the repository)"
        UNSEEN+=("$c")
      fi
    done
  fi
fi
# A NEW ruleset would require these names from every pull request, Factory's included, with no
# bypass actor: a typo then blocks the repository for good. An existing ruleset already requires
# its own names (they must equal these, see below), so only the new-ruleset path needs the opt-in.
if [ -z "$RS_ID" ] && [ "${#UNSEEN[@]}" -gt 0 ] && [ "$ALLOW_UNSEEN" -eq 0 ]; then
  say "Not seen on the latest '$BRANCH' commit: $(printf '%s, ' "${UNSEEN[@]}" | sed 's/, $//')"
  stop_or_note 12 "a new ruleset would require check names nothing has reported; confirm them with --allow-unseen-check, or fix the spelling"
fi

# The active rules for the default branch, read strictly: an error is never "no rules".
RULES=""
fetch_rules() { RULES=$(gh api "repos/$FULL/rules/branches/$BRANCH_ENC" 2>/dev/null) && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$RULES"; }
GATE_JQ='[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[]? | select(.context == "factory/gate")]'
REQUIRED_JQ='[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[]? | select(.context != "factory/gate") | .context] | unique | .[]'
# Is the repository already held by an active ruleset?
gate_listed_any() {
  local n
  fetch_rules || die 1 "cannot read the active rules for '$BRANCH' of $FULL (rate limit, outage or missing permission); refusing to guess"
  n=$(jq "$GATE_JQ | length" <<<"$RULES") || die 1 "unexpected answer for the active rules"
  [ "$n" -ge 1 ]
}
# Retry-friendly (step 7 only): true when the gate is listed with the Factory App's id.
gate_listed_pinned() {
  local n
  fetch_rules || return 1
  n=$(jq --arg id "$FACTORY_APP_ID" "$GATE_JQ | map(select((.integration_id|tostring) == \$id)) | length" <<<"$RULES") || return 1
  [ "$n" -ge 1 ]
}

# The workflow file on a branch: prints the decoded content, or fails.
remote_workflow() { # branch
  gh api "repos/$FULL/contents/$WORKFLOW_PATH?ref=$(urlenc "$1")" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null
}
workflow_present() { # branch
  gh api "repos/$FULL/contents/$WORKFLOW_PATH?ref=$(urlenc "$1")" --jq '.sha' >/dev/null 2>&1
}

# ---------------------------------------------------------------- preflight 2: every refusal, before any write
RS_ALREADY_MATCHES=0
if ! workflow_present "$BRANCH"; then
  if gate_listed_any; then
    die 4 "$FULL is already held by an active ruleset ($GATE required) but has no enrolment workflow: a change to it could never merge. Exclude the repository from the ruleset temporarily (an owner), then re-run."
  fi
  if [ "$SCOPES_KNOWN" -eq 1 ] && ! have_scope workflow; then
    die 3 "creating $WORKFLOW_PATH needs the 'workflow' scope on your gh token (this is your token, not the Factory App): gh auth refresh -s workflow"
  fi
fi
if [ "$CI_CONFIRMED" -eq 1 ] && [ -n "$RS_ID" ]; then
  [ "$(jq -r '.conditions.repository_name != null' <<<"$RS_JSON")" = "true" ] \
    || die 4 "ruleset '$RULESET' selects repositories by something other than repository_name (id/property); add $REPO to it by hand"
  RS_CHECKS=$(jq -r '[.rules[]? | select(.type == "required_status_checks") | .parameters.required_status_checks[]? | select(.context != "factory/gate") | .context] | unique | .[]' <<<"$RS_JSON") \
    || die 3 "cannot read the required checks of ruleset '$RULESET'"
  if [ "$RS_CHECKS" != "$MY_CHECKS" ]; then
    say "ruleset requires:  $(printf '%s' "$RS_CHECKS" | paste -sd, - | sed 's/,/, /g')"
    say "repository CI:     $(printf '%s' "$MY_CHECKS" | paste -sd, - | sed 's/,/, /g')"
    die 4 "the ruleset's required check names differ from this repository's CI checks. Every listed check is required in every matched repository, so this repository would be blocked or under-checked. Re-run with --new-ruleset (and --ruleset NAME) to give this group its own ruleset."
  fi
  pinned=$(jq -r --arg id "$FACTORY_APP_ID" '[.rules[]? | select(.type == "required_status_checks") | .parameters.required_status_checks[]? | select(.context == "factory/gate" and ((.integration_id|tostring) == $id))] | length' <<<"$RS_JSON") \
    || die 3 "cannot read the required checks of ruleset '$RULESET'"
  [ "$pinned" -ge 1 ] || die 4 "ruleset '$RULESET' does not require $GATE from the Factory App (id $FACTORY_APP_ID); refusing to extend it"

  in_list() { # kind (include|exclude)
    local p
    while IFS= read -r p; do
      [ -n "$p" ] || continue
      # shellcheck disable=SC2053 # $p is a ruleset repository_name pattern (fnmatch): a glob match is intended
      if [ "$p" = "~ALL" ] || [[ $REPO == $p ]]; then return 0; fi
    done < <(jq -r --arg k "$1" '.conditions.repository_name[$k][]?' <<<"$RS_JSON")
    return 1
  }
  if in_list exclude; then
    die 4 "$REPO is excluded by ruleset '$RULESET'; remove the exclusion by hand (an owner) if that is intended"
  elif in_list include; then
    RS_ALREADY_MATCHES=1
  fi
fi

# ---------------------------------------------------------------- step 1: repository settings
step "1: repository settings (squash merge + auto-merge)"
if [ "$ALLOW_SQUASH" = "true" ] && [ "$ALLOW_AUTO" = "true" ]; then
  say "ok: squash merge and auto-merge are already enabled"
else
  do_write "enable squash merging and auto-merge" gh repo edit "$FULL" --enable-squash-merge --enable-auto-merge \
    || die 1 "could not change the repository settings"
fi

# ---------------------------------------------------------------- step 2: install the two Apps
step "2: install the Factory App and the merge-enrolment App on the repository"
MANUAL_URLS=()
ensure_installed() { # label installation_id selection
  local label=$1 inst=$2 sel=$3 listed
  if [ "$sel" = "all" ]; then
    say "ok: $label is installed on all repositories of $ORG"
    return 0
  fi
  if listed=$(gh api --paginate "user/installations/$inst/repositories" --jq '.repositories[].id' 2>/dev/null); then
    if grep -qxF "$REPO_ID" <<<"$listed"; then
      say "ok: $label is already installed on $FULL"
      return 0
    fi
  else
    say "note: cannot list the repositories of the $label installation; the add below is idempotent"
  fi
  if do_write "install the $label on $FULL" gh api -X PUT "user/installations/$inst/repositories/$REPO_ID" --silent; then
    return 0
  fi
  warn "GitHub refused the API call for the $label"
  MANUAL_URLS+=("$label: open https://github.com/organizations/$ORG/settings/installations/$inst , under 'Repository access' add $FULL, Save")
}
ensure_installed "Factory App" "$FACTORY_INST" "$FACTORY_SEL"
ensure_installed "merge-enrolment App" "$MERGE_INST" "$MERGE_SEL"
if [ "${#MANUAL_URLS[@]}" -gt 0 ]; then
  say "MANUAL STEP NEEDED:"
  printf '  - %s\n' "${MANUAL_URLS[@]}"
  if [ "$CONTINUE_MANUAL" -eq 1 ]; then
    say "--continue-after-manual: continuing; make sure the installation is done before the first pull request"
  else
    stop_or_note 10 "manual step needed: install the App(s) as listed above, then re-run this script"
  fi
fi

# ---------------------------------------------------------------- step 3: organisation secret
step "3: make $SECRET_NAME available to the repository (the secret value is never read)"
if [ "$SEC_ADD" -eq 1 ]; then
  do_write "add $FULL to the selected repositories of $SECRET_NAME" \
    gh api -X PUT "orgs/$ORG/actions/secrets/$SECRET_NAME/repositories/$REPO_ID" --silent \
    || die 1 "could not add the repository to $SECRET_NAME"
else
  say "ok: $SEC_NOTE"
fi

# ---------------------------------------------------------------- step 4: the enrolment workflow
step "4: enrolment workflow on the default branch ($WORKFLOW_MODE mode), BEFORE the ruleset"
WORKFLOW_ASSUMED=0
if workflow_present "$BRANCH"; then
  say "ok: $WORKFLOW_PATH already exists on '$BRANCH'"
  remote=$(remote_workflow "$BRANCH" || true)
  if [ "$(printf '%s' "$remote")" != "$(printf '%s' "$WORKFLOW_CONTENT")" ]; then
    warn "the file on '$BRANCH' differs from the rendered template; it is NOT touched (once the ruleset holds the repository a change needs factory/gate). Review it by hand."
  fi
  case "$remote" in
    *'<<'*) die 3 "the workflow on '$BRANCH' still contains an unreplaced '<<' placeholder; fix it by hand before the ruleset" ;;
  esac
else
  if [ "$WORKFLOW_MODE" = "direct" ]; then
    do_write "commit $WORKFLOW_PATH to '$BRANCH'" \
      gh api -X PUT "repos/$FULL/contents/$WORKFLOW_PATH" --silent \
      -f message="Add the Factory auto-merge enrolment workflow" -f content="$WORKFLOW_B64" -f branch="$BRANCH" \
      || die 1 "could not commit the workflow to '$BRANCH' (branch protection? use --workflow-mode pr)"
    if [ "$APPLY" -eq 1 ]; then
      workflow_present "$BRANCH" || die 1 "the workflow is not on '$BRANCH' after the commit"
      say "verified: $WORKFLOW_PATH is on '$BRANCH'"
    else
      WORKFLOW_ASSUMED=1
    fi
  else
    pr_url=$(gh api "repos/$FULL/pulls?state=open&head=$(urlenc "$ORG:$ENROLL_BRANCH")" --jq '.[0].html_url // empty' 2>/dev/null || true)
    if [ -n "$pr_url" ]; then
      say "an enrolment pull request is already open: $pr_url"
      stop_or_note 11 "merge the pull request $pr_url, then re-run this script"
      WORKFLOW_ASSUMED=1
    else
      base_sha=$(gh api "repos/$FULL/git/ref/heads/$BRANCH" --jq '.object.sha') || die 1 "cannot read the head of '$BRANCH'"
      if gh api "repos/$FULL/git/ref/heads/$ENROLL_BRANCH" --jq '.object.sha' >/dev/null 2>&1; then
        say "ok: branch $ENROLL_BRANCH already exists (it is reused, never deleted or force-pushed)"
      else
        do_write "create branch $ENROLL_BRANCH at $BRANCH" \
          gh api -X POST "repos/$FULL/git/refs" --silent -f ref="refs/heads/$ENROLL_BRANCH" -f sha="$base_sha" \
          || die 1 "could not create branch $ENROLL_BRANCH"
      fi
      if workflow_present "$ENROLL_BRANCH"; then
        say "ok: $WORKFLOW_PATH is already on $ENROLL_BRANCH"
      else
        do_write "commit $WORKFLOW_PATH to $ENROLL_BRANCH" \
          gh api -X PUT "repos/$FULL/contents/$WORKFLOW_PATH" --silent \
          -f message="Add the Factory auto-merge enrolment workflow" -f content="$WORKFLOW_B64" -f branch="$ENROLL_BRANCH" \
          || die 1 "could not commit the workflow to $ENROLL_BRANCH"
      fi
      do_write "open the enrolment pull request into '$BRANCH'" \
        gh api -X POST "repos/$FULL/pulls" --jq '.html_url' \
        -f title="Add the Factory auto-merge enrolment workflow" -f head="$ENROLL_BRANCH" -f base="$BRANCH" \
        -f body="Installs handoff/factory-automerge.yml (docs/handoff.md). Merge this BEFORE the repository is added to the Factory ruleset." \
        || die 1 "could not open the pull request"
      say "pull request: ${LAST_OUT:-<url shown after --apply>}"
      stop_or_note 11 "merge the pull request ${LAST_OUT:-} into '$BRANCH', then re-run this script"
      WORKFLOW_ASSUMED=1
    fi
  fi
fi

# ---------------------------------------------------------------- step 5: CI check names
step "5: CI check names"
if [ "$CI_CONFIRMED" -eq 0 ]; then
  say "not confirmed (see step 0): ruleset steps skipped in this dry run"
else
  say "confirmed: ${CI_CHECKS[*]:-(none)}"
fi

# ---------------------------------------------------------------- step 6: ruleset
step "6: ruleset '$RULESET'"
if [ "$CI_CONFIRMED" -eq 0 ]; then
  say "skipped (CI check names not confirmed)"
else
  # Never touch the ruleset unless the workflow is really on the default branch.
  if [ "$APPLY" -eq 1 ]; then
    workflow_present "$BRANCH" || die 1 "the workflow is not on '$BRANCH'; refusing to touch the ruleset"
    say "verified: $WORKFLOW_PATH is on '$BRANCH'"
  elif [ "$WORKFLOW_ASSUMED" -eq 1 ]; then
    say "(dry run: assuming the workflow is on '$BRANCH' by now)"
  fi

  if [ -n "$RS_ID" ]; then
    # The refusals (check names, exclusion, pinning, conditions) were decided in the preflight.
    rs_enf=$(jq -r '.enforcement' <<<"$RS_JSON") || die 1 "cannot read the ruleset"
    [ "$rs_enf" = "active" ] || warn "ruleset '$RULESET' enforcement is '$rs_enf', not active; the final verification will fail until it is active"
    n_bypass=$(jq '(.bypass_actors // []) | length' <<<"$RS_JSON") || die 1 "cannot read the ruleset"
    if [ "$n_bypass" -gt 0 ]; then
      warn "ruleset '$RULESET' has $n_bypass bypass actor(s): they can merge around the gate and CI. This script adds none and does not remove any; docs/handoff.md says keep bypass_actors empty."
    fi
    if [ "$RS_ALREADY_MATCHES" -eq 1 ]; then
      say "ok: ruleset '$RULESET' already matches $REPO and its required checks equal the repository's CI checks"
    else
      BODY=$(jq --arg r "$REPO" '{name, target, enforcement, bypass_actors: (.bypass_actors // []), conditions: (.conditions | .repository_name.include += [$r]), rules}' <<<"$RS_JSON")
      do_write "add $REPO to conditions.repository_name.include of ruleset '$RULESET'" \
        gh api -X PUT "orgs/$ORG/rulesets/$RS_ID" --input - --silent \
        || die 1 "could not update the ruleset"
    fi
  else
    # --new-ruleset: from handoff/ruleset.json; never any bypass actor.
    BODY=$(jq --arg name "$RULESET" --arg r "$REPO" --argjson app "$FACTORY_APP_ID" --argjson checks "$CHECKS_JSON" '
        .name = $name
        | .bypass_actors = []
        | .conditions.repository_name.include = [$r]
        | (.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks)
            = (($checks | map({context: .})) + [{context: "factory/gate", integration_id: $app}])' \
        "$HANDOFF_DIR/ruleset.json") || die 1 "could not render handoff/ruleset.json"
    case "$BODY" in *'<<'*) BODY=""; die 1 "the ruleset payload still contains '<<'; refusing" ;; esac
    do_write "create ruleset '$RULESET' for $REPO only (from handoff/ruleset.json, no bypass actors)" \
      gh api -X POST "orgs/$ORG/rulesets" --input - --silent \
      || die 1 "could not create the ruleset"
  fi
fi

# ---------------------------------------------------------------- step 7: verify
step "7: verify what the workflow will see"
if [ "$CI_CONFIRMED" -eq 0 ]; then
  say "skipped (CI check names not confirmed)"
elif [ "$APPLY" -eq 0 ]; then
  say "DRY-RUN  would check: gh api repos/$FULL/rules/branches/$BRANCH_ENC lists $GATE with integration_id $FACTORY_APP_ID"
else
  # What GitHub will really enforce on a pull request: the gate from the Factory App, and EXACTLY the
  # confirmed CI checks (a second ruleset may add a check this script never saw).
  ok=0 why="the gate is not listed"
  for attempt in 1 2 3; do
    if gate_listed_pinned; then
      effective=$(jq -r "$REQUIRED_JQ" <<<"$RULES") || die 1 "unexpected answer for the active rules"
      if [ "$effective" = "$MY_CHECKS" ]; then ok=1; break; fi
      why="the active rules also require: $(printf '%s' "$effective" | paste -sd, - | sed 's/,/, /g') (confirmed: $(printf '%s' "$MY_CHECKS" | paste -sd, - | sed 's/,/, /g'))"
    fi
    if [ "$attempt" -lt 3 ]; then sleep "$RETRY_SLEEP"; fi
  done
  [ "$ok" -eq 1 ] || die 5 "verification failed ($why): the active rules for '$BRANCH' of $FULL do not list $GATE with the Factory App id $FACTORY_APP_ID plus exactly the confirmed checks (ruleset not active, pattern does not match, or another ruleset adds checks)"
  say "verified: $GATE is required from App id $FACTORY_APP_ID on '$BRANCH', with exactly the confirmed checks"
fi

# ---------------------------------------------------------------- step 8: what remains
step "8: remaining manual steps (Factory configuration)"
cat <<SNIPPET
Add this entry to repo_configs in INFRA_TFVARS (placeholders in <<...>> are yours to fill):

  "$FULL" = <<-TOML
    repo            = "$FULL"
    base_branch     = "$BRANCH"
    installation_id = $FACTORY_INST

    [worker]
    uid = 10002
    gid = 10010

    [sandbox]
    verify_domains = ["<<VERIFY_DOMAIN>>"]

    [preflight]
    probe_url = "https://<<VERIFY_DOMAIN>>/"
  TOML

Then, in this order (every repo_configs change is a new rollout):
  1. drain the manager (nothing in flight), see docs/operations.md
  2. update INFRA_TFVARS and apply it with your own pipeline (terraform -chdir=infra apply)
  3. check that GET /repos on the manager lists $FULL without an error field and with "github": "ok"
     ("checking" right after a manager start; "permission missing: ..." means the installation has
     not accepted that permission yet)
SNIPPET

if [ "$APPLY" -eq 0 ]; then
  say ""
  say "DRY RUN complete: nothing was written. Re-run with --apply to perform the steps above."
fi
exit 0
