"""Execution-grounded memory: a model may not remember an outcome that did not happen.

The invariant (graduation hardening, 2026-09-26)
------------------------------------------------
A model-authored ``WRITE_MEMORY`` becomes evidence only if **every
side-effecting intent of the same decision reached ``EXECUTED``**.

Why this shape
--------------
Observed live on staging: a Scout's decision carried two intents,
``CREATE_IMPROVEMENT`` and ``WRITE_MEMORY`` ("new proposal raised"). The
proposal was refused ("portfolio full"); the memory was written anyway, and
the next Scout run declined the same critical signal as a "duplicate of the
06:02 proposal" -- a proposal that never existed. The model authored both
intents in ONE decision, before either executed, so the memory could only ever
state what the model *expected* to happen. Nothing a model writes about the
decision it is making can be evidence about that decision's outcome.

The rule is therefore about execution, not wording. There is no phrase
matching and no payload field a model could set to opt out:

* the memory's precondition is every side-effecting sibling intent of its
  run; a sibling that FAILED, was DENIED, is AWAITING_APPROVAL / APPROVED but
  not yet resumed, was REJECTED or SKIPPED, or is still PENDING, means the
  memory is refused (its intent FAILS with the trusted reason, so the refusal
  is itself recorded and shown back to the agent as a recent refusal);
* memory intents run after every other intent of their run (the worker
  orders them), so the outcome is known when the check runs; the check lives
  in the executor, so a memory resumed through the approval path is held to
  it too;
* observation and hypothesis memories are unaffected when their decision had
  no side effect, or when every side effect executed. Read-only repository
  intelligence and ``SLEEP`` are not side effects; any other intent type --
  including an unknown or invalid one the model emitted -- is, so the rule
  fails closed.

Trusted outcome facts need no model: ``AgentIntent`` rows (and the
``recent_refusals`` context block derived from them) already record what
executed and what did not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

from sqlalchemy.orm import Session

from ..models import AgentIntent, IntentExecutionStatus
from .intents import REPO_READ_INTENT_TYPES, IntentType

#: Intent types that change nothing outside the memory store or the run's own
#: pacing. Everything else -- including a type the model invented -- is a side effect.
NON_SIDE_EFFECT_TYPES = frozenset(
    {IntentType.WRITE_MEMORY.value, IntentType.SLEEP.value} | {t.value for t in REPO_READ_INTENT_TYPES}
)
EXECUTED = IntentExecutionStatus.EXECUTED.value


def is_side_effecting(intent_type: str) -> bool:
    return str(intent_type) not in NON_SIDE_EFFECT_TYPES


def _status(value) -> str:
    return str(getattr(value, "value", value))


@dataclass(frozen=True)
class Grounding:
    admitted: bool
    reason: str
    #: (seq, intent_type, execution_status) of every side-effecting sibling
    siblings: Tuple[Tuple[int, str, str], ...] = ()


def decide(memory_seq: int, siblings: Iterable[Tuple[int, str, str]]) -> Grounding:
    """Pure decision over ``(seq, intent_type, execution_status)`` rows of the
    memory's run (the memory itself may be included; it is ignored)."""
    side = tuple(sorted((int(s), str(t), _status(st)) for s, t, st in siblings if int(s) != int(memory_seq) and is_side_effecting(t)))
    unmet = [(s, t, st) for s, t, st in side if st != EXECUTED]
    if unmet:
        detail = "; ".join(f"seq {s} {t} is {st}" for s, t, st in unmet)
        return Grounding(
            admitted=False,
            reason=(
                "memory not grounded: a memory written in the same decision as a side effect is admitted only "
                f"after every side effect executed ({detail})"
            ),
            siblings=side,
        )
    return Grounding(admitted=True, reason="grounded" if side else "no side effect in this decision", siblings=side)


def check(db: Session, *, run_id: uuid.UUID, memory_intent_id: uuid.UUID, memory_seq: int) -> Grounding:
    """Read the memory's siblings from durable intent rows and decide."""
    rows: Sequence[Tuple[int, str, object]] = (
        db.query(AgentIntent.seq, AgentIntent.intent_type, AgentIntent.execution_status)
        .filter(AgentIntent.run_id == run_id, AgentIntent.id != memory_intent_id)
        .all()
    )
    return decide(memory_seq, rows)


def execution_order_key(intent_type: str, seq: int) -> Tuple[int, int]:
    """Sort key for a run's pending intents: memories after everything else,
    each group in the model's order."""
    return (1 if str(intent_type) == IntentType.WRITE_MEMORY.value else 0, int(seq))


__all__ = ["Grounding", "NON_SIDE_EFFECT_TYPES", "check", "decide", "execution_order_key", "is_side_effecting"]
