"""Choose the Claude model and effort: repository defaults and per-issue labels.

Precedence for model and effort, most specific first:

1. the issue's labels ``factory-model:<alias or id>`` and ``factory-effort:<level>`` (at most one
   of each), for the **build and verify** phases only: the reviewer stays on the repository's
   configuration (it is meant to be independent of the builder) and the planner runs before any
   job exists;
2. the repository's per-phase ``[models]`` and ``[efforts]``;
3. the repository's ``model`` and ``effort`` defaults (plan, build, review and verify; never the
   preflight probe, see ``phases.DEFAULT_PHASES``);
4. the built-in phase table (``phases.POLICIES``).

A label is a person's request, never a trusted value. Applying a label needs only triage access,
so the choice labels carry the trigger's own bar: the newest applier of each must hold at least
``auto_build_min_permission`` (``trigger.LabelAuthority``, injected as ``authority``; no
``authority``, or one that cannot say yes, refuses the label). The value is then validated against
what the operator allows (``allowed_models``, ``max_effort``, ``[aliases]``) when the issue is
planned, and what is not authorised, valid or allowed is **not** silently replaced by a default: the
issue gets a ``blocked`` disposition with a fixed reason (``issue_comments`` then tells the person),
no job and no worker. Reasons and log lines carry fixed codes only, never the label's text.

The resolved model, effort and where each came from are recorded per job in ``plan.json``
(``choice``, validated by ``check_choice``), resolved once at planning time: changing an alias
target or the repository configuration later never changes a run that is already planned. Jobs
planned before this existed have no ``choice`` and are resolved from the live options as before.

No GitHub or process access lives here: the label names come from the issue snapshot and who applied
them from ``authority``.
"""

import re
from copy import deepcopy
from dataclasses import dataclass

from .phases import DEFAULT_PHASES, EFFORTS, ISSUE_PHASES, POLICIES, policy_for

MODEL_LABEL_PREFIX = "factory-model:"
EFFORT_LABEL_PREFIX = "factory-effort:"
# Fixed and one-to-one (no prefix or fuzzy matching); a repository may redefine them or add its own
# in ``[aliases]``. The targets are the ids the phase table uses.
DEFAULT_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
# A model id as the repository config accepts it (``models.<phase>``, ``model``).
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,127}")
# An alias is a lowercase label suffix: at most 36 characters, so the whole label fits GitHub's 50.
ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,35}")

# Where a value came from, from least to most specific.
SOURCES = ("built-in", "repository", "repository-phase", "issue")
SOURCE_TEXT = {
    "built-in": "built-in default",
    "repository": "repository default",
    "repository-phase": "repository per-phase setting",
    "issue": "issue label",
}
_CHOICE_KEYS = {"model", "effort", "model_source", "effort_source"}

# name -> sentence. Fixed text: nothing of the label is echoed. ``blocked_reason`` joins them.
REASONS = {
    "label-not-authorised": (
        "a factory-model or factory-effort label was not applied by someone who may start a build here "
        "(auto_build_min_permission), or who applied it could not be established"
    ),
    "duplicate-model-label": "more than one factory-model label is on the issue (at most one is allowed)",
    "duplicate-effort-label": "more than one factory-effort label is on the issue (at most one is allowed)",
    "unknown-model": "a factory-model label does not name a model alias or a model id",
    "model-not-allowed": (
        "a factory-model label names a model that is neither an alias of this repository "
        "nor in its allowed_models"
    ),
    "unknown-effort": f"a factory-effort label is not one of {', '.join(EFFORTS)}",
    "effort-above-max": (
        "a factory-effort label is above the highest effort an issue may select here (max_effort)"
    ),
    "conflicting-directives": (
        "the issues the planner grouped into one job carry different factory-model or "
        "factory-effort labels, and one job runs with one choice"
    ),
    "dependency-blocked": (
        "a job it depends on was blocked because of conflicting factory-model or factory-effort labels"
    ),
}
MAX_REASON = 480


@dataclass(frozen=True)
class Directive:
    """What an issue asks for: a full model id (an alias already resolved) and/or an effort."""

    model: str | None = None
    effort: str | None = None

    @property
    def empty(self):
        return self.model is None and self.effort is None

    def token(self):
        """A fixed-alphabet summary for the planner index: validated values only, never label text."""
        return f"{self.model or 'default'}/{self.effort or 'default'}"


EMPTY = Directive()


def alias_table(options):
    """The aliases in force: the three built-in ones, overridden or extended by ``[aliases]``."""
    return {**DEFAULT_ALIASES, **(options.aliases or {})}


def allowed_models(options):
    """The ids an issue may select. An explicit ``allowed_models`` is exact; the default is the
    built-in table's ids plus everything the repository itself configures (``model``, ``[models]``
    and the ``[aliases]`` targets), so a repository that sets ``model = X`` can select X."""
    if options.allowed_models is not None:
        return frozenset(options.allowed_models)
    ids = {policy.model for policy in POLICIES.values()}
    ids.update(DEFAULT_ALIASES.values())
    if options.default_model:
        ids.add(options.default_model)
    ids.update(options.models.values())
    ids.update((options.aliases or {}).values())
    return frozenset(ids)


def effort_ceiling(options):
    """The highest effort an issue may select: ``max_effort``, else the highest the built-in table
    or the repository's own ``effort`` / ``[efforts]`` already use."""
    if options.max_effort is not None:
        return options.max_effort
    used = {policy.effort for policy in POLICIES.values()}
    if options.default_effort:
        used.add(options.default_effort)
    used.update(options.efforts.values())
    return max(used, key=EFFORTS.index)


def _label_names(issue):
    labels = issue.get("labels") if isinstance(issue, dict) else None
    names = []
    for label in labels if isinstance(labels, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name.strip())
    return names


def _in_family(name, prefix):
    """Whether a label name starts with ``prefix`` (compared case-insensitively)."""
    return name[: len(prefix)].casefold() == prefix


def choice_labels(issue):
    """The names of the issue's labels that choose a model or an effort (either family), as they are written."""
    return [
        name
        for name in _label_names(issue)
        if _in_family(name, MODEL_LABEL_PREFIX) or _in_family(name, EFFORT_LABEL_PREFIX)
    ]


def _values(names, prefix):
    """The text after ``prefix`` of every label of that family (prefix compared case-insensitively)."""
    return [name[len(prefix) :].strip() for name in names if _in_family(name, prefix)]


def _model(value, options):
    key = value.casefold()
    aliases = alias_table(options)
    if key in aliases:
        model = aliases[key]
    elif MODEL_ID.fullmatch(value):
        model = value
    else:
        return None, "unknown-model"
    if model not in allowed_models(options):
        return None, "model-not-allowed"
    return model, None


def _effort(value, options):
    key = value.casefold()
    if key not in EFFORTS:
        return None, "unknown-effort"
    if EFFORTS.index(key) > EFFORTS.index(effort_ceiling(options)):
        return None, "effort-above-max"
    return key, None


def issue_directive(issue, options):
    """``(Directive, ())`` for an issue's labels, or ``(None, codes)`` when any of them is refused.

    No choice label at all is a valid empty directive. Two model labels (or two effort labels) are
    refused, never resolved by picking one.
    """
    names = _label_names(issue)
    models = _values(names, MODEL_LABEL_PREFIX)
    efforts = _values(names, EFFORT_LABEL_PREFIX)
    codes = []
    model = effort = None
    if len(models) > 1:
        codes.append("duplicate-model-label")
    elif models:
        model, code = _model(models[0], options)
        if code:
            codes.append(code)
    if len(efforts) > 1:
        codes.append("duplicate-effort-label")
    elif efforts:
        effort, code = _effort(efforts[0], options)
        if code:
            codes.append(code)
    if codes:
        return None, tuple(codes)
    return Directive(model, effort), ()


def blocked_reason(codes):
    """The fixed disposition reason for refused codes (at most ``MAX_REASON`` characters)."""
    problems = "; ".join(REASONS[code] for code in codes)
    text = f"Not planned: {problems}. Nothing was changed for this issue: fix or remove the label, then ask for a new plan."
    return text[:MAX_REASON]


def blocked(number, codes):
    return {"issue": number, "status": "blocked", "reason": blocked_reason(codes)}


def screen(issues, options, authority=None):
    """``(directives, refused)`` for the captured issues: issue number -> ``Directive`` for those
    whose labels are authorised and valid, issue number -> codes for those that are refused.

    ``authority(number, labels)`` says whether the newest applier of every one of those choice labels
    may choose (``trigger.LabelAuthority``). It is asked only for an issue that carries a choice
    label, before the values are looked at, and without it (or on False) the issue is refused with
    ``label-not-authorised`` alone: nobody's request is honoured on a guess.
    """
    directives, refused = {}, {}
    for issue in issues:
        labels = choice_labels(issue)
        if labels and not (authority is not None and authority(issue["number"], labels)):
            refused[issue["number"]] = ("label-not-authorised",)
            continue
        directive, codes = issue_directive(issue, options)
        if codes:
            refused[issue["number"]] = codes
        else:
            directives[issue["number"]] = directive
    return directives, refused


def phase_choice(phase, options, directive=EMPTY):
    """The resolved model and effort of one phase, with the source of each."""
    policy = policy_for(phase, options)
    if directive.model is not None and phase in ISSUE_PHASES:
        model, model_source = directive.model, "issue"
    else:
        model, model_source = policy.model, _source(phase, options, "model")
    if directive.effort is not None and phase in ISSUE_PHASES:
        effort, effort_source = directive.effort, "issue"
    else:
        effort, effort_source = policy.effort, _source(phase, options, "effort")
    return {
        "model": model,
        "effort": effort,
        "model_source": model_source,
        "effort_source": effort_source,
    }


def _source(phase, options, field):
    table = options.models if field == "model" else options.efforts
    default = options.default_model if field == "model" else options.default_effort
    if phase in table:
        return "repository-phase"
    if phase in DEFAULT_PHASES and default is not None:
        return "repository"
    return "built-in"


def job_choice(options, directive=EMPTY):
    """The ``choice`` recorded on a job: build and verify, each with model, effort and sources."""
    return {phase: phase_choice(phase, options, directive) for phase in ISSUE_PHASES}


def assign(jobs, dispositions, directives, options):
    """``(jobs, dispositions)`` with every job's ``choice`` recorded.

    Whatever ``choice`` the planner wrote is dropped: only Factory resolves it. One job runs with
    one choice, so a job whose issues carry different directives is not run: its issues and, one
    by one, the issues of every job that depends on it get a ``blocked`` disposition instead.
    """
    bare = [{key: value for key, value in job.items() if key != "choice"} for job in jobs]
    dropped = {}
    for job in bare:
        if len({directives.get(number, EMPTY) for number in job["issues"]}) > 1:
            dropped[job["id"]] = "conflicting-directives"
    grew = True
    while grew:
        grew = False
        for job in bare:
            if job["id"] not in dropped and any(d in dropped for d in job.get("depends_on", [])):
                dropped[job["id"]] = "dependency-blocked"
                grew = True
    kept, replaced = [], {}
    for job in bare:
        if job["id"] in dropped:
            for number in job["issues"]:
                replaced[number] = blocked(number, (dropped[job["id"]],))
        else:
            directive = directives.get(job["issues"][0], EMPTY)
            kept.append({**job, "choice": job_choice(options, directive)})
    return kept, [replaced.get(item["issue"], item) for item in dispositions]


def check_choice(value):
    """Raise ``ValueError`` (a fixed message) unless ``value`` is a well-formed recorded ``choice``."""
    if not isinstance(value, dict) or set(value) != set(ISSUE_PHASES):
        raise ValueError("invalid recorded model choice")
    for entry in value.values():
        if (
            not isinstance(entry, dict)
            or set(entry) != _CHOICE_KEYS
            or not isinstance(entry["model"], str)
            or not MODEL_ID.fullmatch(entry["model"])
            or entry["effort"] not in EFFORTS
            or entry["model_source"] not in SOURCES
            or entry["effort_source"] not in SOURCES
        ):
            raise ValueError("invalid recorded model choice")
    return value


def recorded_choice(job):
    """A copy of the valid ``choice`` recorded on a planned job, or None (no record, or not well formed)."""
    try:
        return deepcopy(check_choice(job.get("choice"))) if "choice" in job else None
    except (ValueError, AttributeError):
        return None


def phase_override(job, phase):
    """The ``{"model", "effort"}`` recorded for ``phase`` of a planned job, or None.

    None for any phase an issue does not choose for, and for a job without a (valid) record: the
    live options then decide, exactly as before this existed.
    """
    choice = job.get("choice") if isinstance(job, dict) else None
    if phase not in ISSUE_PHASES:
        return None
    try:
        entry = check_choice(choice)[phase]
    except ValueError:
        return None
    return {"model": entry["model"], "effort": entry["effort"]}


def describe(entry, long=False):
    """``claude-sonnet-5-5 (built-in), effort high (issue)`` for one recorded phase entry."""
    text = SOURCE_TEXT if long else {key: key for key in SOURCES}
    return (
        f"{entry['model']} ({text[entry['model_source']]}), "
        f"effort {entry['effort']} ({text[entry['effort_source']]})"
    )


def label_styles(options):
    """``name -> (colour, description)`` of the labels an issue may carry: one per alias, one per effort.

    Fixed colours and descriptions, so ``gh label create --force`` is idempotent and never recolours
    a label an operator looks at.
    """
    styles = {}
    for alias in sorted(alias_table(options)):
        styles[f"{MODEL_LABEL_PREFIX}{alias}"] = (
            "bfd4f2",
            f"Factory: build and verify this issue with the {alias} model",
        )
    for effort in EFFORTS:
        styles[f"{EFFORT_LABEL_PREFIX}{effort}"] = (
            "d4c5f9",
            f"Factory: build and verify this issue at {effort} effort",
        )
    return styles
