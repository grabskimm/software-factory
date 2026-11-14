"""Which backend runs a repository's worker sessions: the ``execution`` setting and the manager switch.

This is slice 12a of the pod backend (``docs/pod-backend.md``) and the one place slice 12b is selected. It
chooses; it launches nothing itself.

* ``execution`` (``config.RepoConfig``) is per repository: ``inprocess`` (the default) runs every
  session as a child of the supervisor, exactly as before; ``pod`` asks for a pod per session. The
  choice covers every phase (plan, build, review, verify and the preflight probe), with no per-phase
  split, and the three callers of ``agent.run_agent`` (``Runner.invoke``, the planner and the
  preflight probe) all read it through ``agent_kwargs``.
* ``FACTORY_POD_BACKEND`` is the manager-wide switch (``podcontract.ENV_MANAGER["pod_backend"]``).
  Only the exact value ``on`` turns it on; unset, ``off`` and anything else is off, so a typo fails
  closed. The manager passes its environment to every supervisor it starts, so a supervisor reads the
  same switch.

``execution = "pod"`` is refused, and nothing is launched, in every case but the last below. There is
deliberately no silent fallback to in-process execution: that would run the workers of a repository
that asked for pods inside the manager pod. The owner removes ``execution = "pod"`` from the
repository config to go back.

* The switch is not ``on``: refused with ``REFUSED_SWITCH_OFF``.
* The switch is ``on`` but the pod backend cannot be built for this session (the supervisor did not wire it in
  with ``activate``, so it has no repository and run; no manager instance id; a launcher socket, spool or data
  directory that is not usable; a job or base that is not valid): refused with ``REFUSED_UNCONFIGURED``.
* The switch is ``on`` and the backend can be built: ``podbackend.PodBackend`` (slice 12b). That includes the process's
  pod pool (``podpool``, built by ``activate``): a supervisor without one refuses rather than runs unguarded.

The refusal is a backend (``RefusedBackend``) whose ``prepare`` raises ``agent.AgentError`` before
anything is written or started, so it travels the error path the callers already handle, with a
fixed text that never contains a value from the configuration, an issue or the environment.

With ``execution = "inprocess"`` ``agent_kwargs`` is empty: the call to ``run_agent`` is the one it
always was and the default ``LocalBackend`` runs it.
"""

import os
from dataclasses import dataclass

from . import podcontract
from .agent import AgentError
from .config import EXECUTION_INPROCESS, EXECUTION_POD

# Fixed texts: no value of the configuration or the environment is ever echoed into them.
_BACK = "Remove execution = \"pod\" from the repository config to run its sessions in process."
REFUSED_SWITCH_OFF = (
    "execution = \"pod\" is refused: the pod backend is switched off on this deployment "
    f"(FACTORY_POD_BACKEND is not on) and nothing was started. {_BACK}"
)
REFUSED_UNCONFIGURED = (
    "execution = \"pod\" is refused: the pod backend is not configured for this session "
    f"(see docs/pod-backend.md) and nothing was started. {_BACK}"
)
REFUSED_UNKNOWN = "execution is refused: it is not one of the known modes and nothing was started."


def switch_is_on(env=None):
    """Whether the manager-wide pod switch is ``on`` in ``env`` (the process environment by default).

    Only the exact value ``on`` is on: unset, ``off``, another spelling and garbage are all off.
    """
    source = os.environ if env is None else env
    return source.get(podcontract.ENV_MANAGER["pod_backend"]) == podcontract.POD_BACKEND_ON


def switch_value(env=None):
    """The switch as the two words the contract defines (``on`` or ``off``)."""
    return podcontract.POD_BACKEND_ON if switch_is_on(env) else podcontract.POD_BACKEND_OFF


@dataclass(frozen=True)
class RefusedBackend:
    """An ``AgentBackend`` that starts nothing: every step raises ``AgentError`` with a fixed text.

    ``prepare`` is the step that fires, before a file is written or a process exists; ``launch`` and
    ``collect`` repeat the refusal for a caller that skipped it.
    """

    reason: str

    def prepare(self, request):
        raise AgentError(self.reason)

    def launch(self, spec, on_start=None):
        raise AgentError(self.reason)

    def collect(self, spec, outcome):
        raise AgentError(self.reason)


def backend_for(execution, env=None, *, job_id=None, attempt=None, base=None, spent_usd=None, budget_usd=None):
    """The backend ``execution`` selects: None (the in-process ``LocalBackend``), the pod backend or one that refuses.

    A value that is not a known mode is refused too (a ``RepoConfig`` cannot hold one; this is for a
    direct caller): the choice fails closed, never to in-process. ``job_id``, ``attempt`` and ``base``
    (the job's base commit) describe the session for the pod backend and mean nothing to any other choice, and so
    do ``spent_usd`` (a number, or a callable that returns the run's cumulative cost: ``Runner.run_cost``) and
    ``budget_usd`` (the repository's ``run_budget_usd`` or None): the pod pool checks the run's budget with them
    (``podpool``). Without them the pool still enforces the ceiling, the per-repository cap, the pacing and the
    breaker, and the budget stays the runner's own drain.
    """
    if execution == EXECUTION_INPROCESS:
        return None
    if execution != EXECUTION_POD:
        return RefusedBackend(REFUSED_UNKNOWN)
    if not switch_is_on(env):
        return RefusedBackend(REFUSED_SWITCH_OFF)
    # Imported here, so that nothing of the pod backend is loaded while the switch is off.
    from . import podbackend

    try:
        return podbackend.PodBackend.from_environment(
            os.environ if env is None else env,
            job_id=job_id,
            attempt=attempt,
            base=base,
            spent_usd=spent_usd,
            budget_usd=budget_usd,
        )
    except podbackend.PodConfigError:
        return RefusedBackend(REFUSED_UNCONFIGURED)


def agent_kwargs(execution, env=None, *, job_id=None, attempt=None, base=None, spent_usd=None, budget_usd=None):
    """The extra keyword arguments of ``run_agent`` for a repository's ``execution``.

    Empty for ``inprocess``, so that call is byte for byte the one made before the setting existed (the
    keywords describe the session for the pod backend and are ignored for it); ``{"backend": ...}`` for
    any other mode. The three callers spread it into their ``run_agent`` call.
    """
    chosen = backend_for(
        execution, env, job_id=job_id, attempt=attempt, base=base, spent_usd=spent_usd, budget_usd=budget_usd
    )
    return {} if chosen is None else {"backend": chosen}


def activate(repository, run_id, env=None):
    """Wire the pod backend into this supervisor process when the manager switch is on; the function that undoes it.

    ``cli.run`` calls this once per command. With the switch off (the default) it does nothing at all and
    imports nothing; with it on, ``podbackend.activate`` installs the launcher client as the reader and the
    terminator of the pod handles (so recovery, ``purge`` and ``status`` can read a recorded pod), registers the
    lease-loss kill hook and remembers the repository and the run.
    """
    if not switch_is_on(env):
        return _nothing
    from . import podbackend

    return podbackend.activate(repository, run_id, env)


def sweeper(env=None):
    """The orphan sweeper (``podsweep.Sweeper``) for ``Manager.pods``, or None.

    None unless the manager switch is ``on`` AND the pod backend can be built from the environment (a usable launcher
    socket path and an absolute spool directory); with the switch off it imports nothing. The manager sets
    ``manager.pods`` to this once, at its start: while it is None nothing sweeps and ``GET /repos`` has no
    ``worker_pods`` key.
    """
    if not switch_is_on(env):
        return None
    from . import podbackend

    return podbackend.build_sweeper(os.environ if env is None else env)


def _nothing():
    return None
