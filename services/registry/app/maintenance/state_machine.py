"""The Repair Case state machine: executable, trusted, total (ADR-0010 D6).

This table IS the specification (docs/MAINTENANCE_STATE_MACHINE.md is
generated prose about it and is checked against it by tests). Every
non-terminal state declares:

* ``allowed``      -- the only states it may move to, and which actor types
                      may move it there;
* ``timeout``      -- how long the case may sit in the state (``deadline_at``);
* ``poll``         -- the cadence of ``next_action_at`` while it waits;
* ``max_tries``    -- how many controller steps may run in the state before
                      ``on_exhausted`` is applied;
* ``on_deadline``  -- the recovery transition the watchdog executes when
                      ``now > deadline_at`` and no lease is valid;
* ``on_exhausted`` -- where the case goes when its tries are used up.

There is no non-terminal state without a deadline and a recovery target, so
there is no way to write a case that waits forever (the DB enforces
``next_action_at``/``deadline_at`` NOT NULL for non-terminal rows too).

Terminal states are final for the machine. ``SAFELY_ESCALATED`` is the one
state with an exit, and only an OWNER action (an approval recorded with
evidence) can take it: the machine never un-escalates itself.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from .taxonomy import ActorType

C, W, A, O, R = ActorType.CONTROLLER, ActorType.WATCHDOG, ActorType.ACTIVITY, ActorType.OWNER, ActorType.RELEASE


class CaseState(str, enum.Enum):
    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    TRIAGED = "TRIAGED"
    DIAGNOSING = "DIAGNOSING"
    PLAN_READY = "PLAN_READY"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    NEEDS_RESCOPE = "NEEDS_RESCOPE"
    PROMOTING = "PROMOTING"
    READY_FOR_RELEASE = "READY_FOR_RELEASE"
    RELEASING = "RELEASING"
    POST_RELEASE_VERIFYING = "POST_RELEASE_VERIFYING"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    # terminal outcomes
    AUTO_REPAIRED = "AUTO_REPAIRED"
    AUTO_ROLLED_BACK = "AUTO_ROLLED_BACK"
    SAFELY_ESCALATED = "SAFELY_ESCALATED"
    CANNOT_REPRODUCE = "CANNOT_REPRODUCE"
    DUPLICATE_RESOLVED = "DUPLICATE_RESOLVED"
    POLICY_REFUSED = "POLICY_REFUSED"


TERMINAL: FrozenSet[CaseState] = frozenset(
    {
        CaseState.AUTO_REPAIRED,
        CaseState.AUTO_ROLLED_BACK,
        CaseState.SAFELY_ESCALATED,
        CaseState.CANNOT_REPRODUCE,
        CaseState.DUPLICATE_RESOLVED,
        CaseState.POLICY_REFUSED,
    }
)
NON_TERMINAL: FrozenSet[CaseState] = frozenset(set(CaseState) - TERMINAL)

#: States in which no change has been merged anywhere yet: a trusted recovery
#: observation here ends the case as CANNOT_REPRODUCE ("no random patch").
PRE_MERGE: FrozenSet[CaseState] = frozenset(
    {
        CaseState.DETECTED,
        CaseState.CONFIRMED,
        CaseState.TRIAGED,
        CaseState.DIAGNOSING,
        CaseState.PLAN_READY,
        CaseState.BUILDING,
        CaseState.VERIFYING,
        CaseState.NEEDS_RESCOPE,
    }
)

#: States whose step calls a model (the cost governor and the cognition kill
#: switch apply to exactly these).
COGNITIVE: FrozenSet[CaseState] = frozenset({CaseState.DIAGNOSING, CaseState.BUILDING, CaseState.VERIFYING, CaseState.NEEDS_RESCOPE})


@dataclass(frozen=True)
class StateSpec:
    timeout: timedelta
    poll: timedelta
    max_tries: int
    on_deadline: CaseState
    on_exhausted: CaseState
    allowed: Mapping[CaseState, FrozenSet[ActorType]] = field(default_factory=dict)
    purpose: str = ""


def _a(*pairs: Tuple[CaseState, Tuple[ActorType, ...]]) -> Dict[CaseState, FrozenSet[ActorType]]:
    return {s: frozenset(actors) for s, actors in pairs}


M = timedelta(minutes=1)
H = timedelta(hours=1)
ESC, CNR = CaseState.SAFELY_ESCALATED, CaseState.CANNOT_REPRODUCE

TABLE: Dict[CaseState, StateSpec] = {
    CaseState.DETECTED: StateSpec(
        timeout=30 * M, poll=1 * M, max_tries=40, on_deadline=CNR, on_exhausted=CNR,
        purpose="confirm the violation from a fresh trusted observation",
        allowed=_a((CaseState.CONFIRMED, (C,)), (CNR, (C, W)), (CaseState.DUPLICATE_RESOLVED, (C,)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.CONFIRMED: StateSpec(
        timeout=24 * H, poll=5 * M, max_tries=400, on_deadline=ESC, on_exhausted=ESC,
        purpose="deterministic triage lanes, then wait for maintenance capacity and budget (priority queue)",
        allowed=_a((CaseState.TRIAGED, (C,)), (CNR, (C, W)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.TRIAGED: StateSpec(
        timeout=10 * M, poll=0 * M, max_tries=3, on_deadline=ESC, on_exhausted=ESC,
        purpose="a maintenance slot is held; schedule diagnosis",
        allowed=_a((CaseState.DIAGNOSING, (C,)), (CNR, (C, W)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.DIAGNOSING: StateSpec(
        timeout=30 * M, poll=0 * M, max_tries=8, on_deadline=ESC, on_exhausted=ESC,
        purpose="DiagnoseIncident + DesignRepair activities produce immutable plan revision 1",
        allowed=_a((CaseState.PLAN_READY, (C, A)), (CNR, (C, W)), (ESC, (C, W, A, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.PLAN_READY: StateSpec(
        timeout=10 * M, poll=0 * M, max_tries=3, on_deadline=ESC, on_exhausted=ESC,
        purpose="trusted risk classification of the plan revision; refuse constitutional scope",
        allowed=_a((CaseState.BUILDING, (C,)), (CaseState.NEEDS_RESCOPE, (C,)), (CNR, (C, W)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.BUILDING: StateSpec(
        timeout=60 * M, poll=0 * M, max_tries=4, on_deadline=CaseState.BUILDING, on_exhausted=CaseState.NEEDS_RESCOPE,
        purpose="bounded RepairAttempt: read, patch, targeted test, feedback, patch",
        allowed=_a((CaseState.VERIFYING, (C, A)), (CaseState.BUILDING, (C, W, A)), (CaseState.NEEDS_RESCOPE, (C, W, A)), (CNR, (C, W)), (ESC, (C, W, A, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.VERIFYING: StateSpec(
        timeout=45 * M, poll=0 * M, max_tries=8, on_deadline=ESC, on_exhausted=ESC,
        purpose="independent QA (trusted tests), ReviewPatch, SecurityReview, trusted risk recompute",
        allowed=_a((CaseState.PROMOTING, (C,)), (CaseState.BUILDING, (C, A)), (CaseState.NEEDS_RESCOPE, (C, A)), (CNR, (C, W)), (ESC, (C, W, A, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.NEEDS_RESCOPE: StateSpec(
        timeout=30 * M, poll=0 * M, max_tries=4, on_deadline=ESC, on_exhausted=ESC,
        purpose="DesignRepair with Builder/QA evidence proposes plan revision N+1 (never widens N)",
        allowed=_a((CaseState.PLAN_READY, (C, A)), (CNR, (C, W)), (ESC, (C, W, A, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.PROMOTING: StateSpec(
        timeout=6 * H, poll=2 * M, max_tries=400, on_deadline=ESC, on_exhausted=ESC,
        purpose="the trusted promotion controller carries the verified patch to main (PR, CI, merge)",
        allowed=_a((CaseState.READY_FOR_RELEASE, (C,)), (CaseState.BUILDING, (C,)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.READY_FOR_RELEASE: StateSpec(
        timeout=2 * H, poll=1 * M, max_tries=200, on_deadline=ESC, on_exhausted=ESC,
        purpose="attest the merged repair and hand it to the Release Controller",
        allowed=_a((CaseState.RELEASING, (C,)), (ESC, (C, W, O)), (CaseState.POLICY_REFUSED, (C, O))),
    ),
    CaseState.RELEASING: StateSpec(
        timeout=4 * H, poll=1 * M, max_tries=500, on_deadline=ESC, on_exhausted=ESC,
        purpose="mirror the Release Controller (verify, preview, production PR, deploy)",
        allowed=_a((CaseState.POST_RELEASE_VERIFYING, (C, R)), (CaseState.RECOVERY_PENDING, (C, R)), (CaseState.AUTO_ROLLED_BACK, (C, R)), (ESC, (C, W, R, O))),
    ),
    CaseState.POST_RELEASE_VERIFYING: StateSpec(
        timeout=1 * H, poll=1 * M, max_tries=200, on_deadline=CaseState.DIAGNOSING, on_exhausted=ESC,
        purpose="the incident's own monitor must show a healthy streak on the released product",
        allowed=_a((CaseState.AUTO_REPAIRED, (C,)), (CaseState.RECOVERY_PENDING, (C, R)), (CaseState.DIAGNOSING, (C, W)), (ESC, (C, W, O))),
    ),
    CaseState.RECOVERY_PENDING: StateSpec(
        timeout=1 * H, poll=1 * M, max_tries=200, on_deadline=ESC, on_exhausted=ESC,
        purpose="the Release Controller restores known-good; public desired state must be healthy",
        allowed=_a((CaseState.AUTO_ROLLED_BACK, (C, R)), (ESC, (C, W, R, O))),
    ),
    # SAFELY_ESCALATED: terminal for the machine. Only an OWNER action with
    # recorded evidence (e.g. the owner merged the AMBER PR, or re-enabled the
    # release switch for this case) resumes the persisted case.
    CaseState.SAFELY_ESCALATED: StateSpec(
        timeout=0 * M, poll=0 * M, max_tries=0, on_deadline=ESC, on_exhausted=ESC,
        purpose="complete review package delivered to the owner; no agent runs",
        allowed=_a((CaseState.READY_FOR_RELEASE, (O,)), (CaseState.PROMOTING, (O,))),
    ),
}

for _t in TERMINAL - {CaseState.SAFELY_ESCALATED}:
    TABLE[_t] = StateSpec(timeout=0 * M, poll=0 * M, max_tries=0, on_deadline=_t, on_exhausted=_t, purpose="terminal outcome", allowed={})


class IllegalTransition(ValueError):
    pass


def is_terminal(state: CaseState) -> bool:
    return state in TERMINAL


def spec(state: CaseState) -> StateSpec:
    return TABLE[state]


def check_transition(src: CaseState, dst: CaseState, actor: ActorType, *, resumable: Optional[bool] = None) -> None:
    """Raise :class:`IllegalTransition` unless ``actor`` may move ``src`` -> ``dst``.

    The escalation exit additionally needs the case to have been escalated as
    resumable (an approval request), never a final escalation."""
    allowed = TABLE[src].allowed.get(dst)
    if not allowed or actor not in allowed:
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed for {actor.value}")
    if src is CaseState.SAFELY_ESCALATED and not resumable:
        raise IllegalTransition("this escalation is final; only a resumable (approval) escalation can be resumed")


def deadline_for(state: CaseState, entered_at, *, case_deadline=None):
    """The state's deadline, never later than the case's hard wall-clock deadline."""
    if state in TERMINAL:
        return None
    d = entered_at + TABLE[state].timeout
    if case_deadline is not None and d > case_deadline:
        d = case_deadline
    return d


def assert_total() -> None:
    """Every non-terminal state has a finite deadline, a recovery target that
    is a legal move for the watchdog, and at least one path to a terminal
    state. Called by tests and at kernel start-up."""
    for s in NON_TERMINAL:
        sp = TABLE[s]
        if sp.timeout.total_seconds() <= 0:
            raise AssertionError(f"{s.value} has no deadline")
        if sp.max_tries <= 0:
            raise AssertionError(f"{s.value} has no try budget")
        if sp.on_deadline not in sp.allowed or ActorType.WATCHDOG not in sp.allowed[sp.on_deadline]:
            raise AssertionError(f"{s.value}: on_deadline {sp.on_deadline.value} is not a legal watchdog move")
        if sp.on_exhausted not in sp.allowed:
            raise AssertionError(f"{s.value}: on_exhausted {sp.on_exhausted.value} is not a legal move")
        if ESC not in sp.allowed and not any(t in TERMINAL for t in sp.allowed):
            raise AssertionError(f"{s.value} cannot reach a terminal state")
    # reachability: every non-terminal state reaches a terminal one
    for s in NON_TERMINAL:
        seen, stack = set(), [s]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(t for t in TABLE[cur].allowed if t not in TERMINAL)
        if not any(t in TERMINAL for x in seen for t in TABLE[x].allowed):
            raise AssertionError(f"{s.value} cannot reach a terminal state")


assert_total()

__all__ = [
    "CaseState",
    "TERMINAL",
    "NON_TERMINAL",
    "PRE_MERGE",
    "COGNITIVE",
    "StateSpec",
    "TABLE",
    "IllegalTransition",
    "is_terminal",
    "spec",
    "check_transition",
    "deadline_for",
    "assert_total",
]


def markdown_table() -> str:
    """The transition table as Markdown (docs/MAINTENANCE_STATE_MACHINE.md is
    generated from this; tests/society/maintenance/test_state_machine.py
    checks the document against the code)."""

    def fmt(td: timedelta) -> str:
        m = int(td.total_seconds() // 60)
        return f"{m // 60}h" if m and m % 60 == 0 else f"{m}m"

    rows = ["| State | Purpose | Timeout | Poll | Max tries | Recovery | Allowed next states (actor) |", "|---|---|---|---|---|---|---|"]
    order = [s for s in CaseState if s in NON_TERMINAL] + [CaseState.SAFELY_ESCALATED]
    for s in order:
        sp = TABLE[s]
        nxt = "; ".join(f"`{d.value}` ({', '.join(sorted(a.value for a in actors))})" for d, actors in sp.allowed.items())
        rec = "owner resume only" if s is CaseState.SAFELY_ESCALATED else f"on deadline -> `{sp.on_deadline.value}`; tries exhausted -> `{sp.on_exhausted.value}`"
        rows.append(f"| `{s.value}` | {sp.purpose} | {fmt(sp.timeout) if s in NON_TERMINAL else '-'} | {fmt(sp.poll) if s in NON_TERMINAL else '-'} | {sp.max_tries or '-'} | {rec} | {nxt} |")
    return "\n".join(rows) + "\n"
