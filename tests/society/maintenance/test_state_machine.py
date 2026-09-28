"""The Repair Case state machine is executable, total and bounded (ADR-0010 D6)."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings, strategies as st

from services.registry.app.maintenance import state_machine as sm
from services.registry.app.maintenance.taxonomy import ActorType

S = sm.CaseState


def test_every_nonterminal_state_has_an_exit_a_deadline_and_a_budget():
    sm.assert_total()
    for s in sm.NON_TERMINAL:
        spec = sm.spec(s)
        assert spec.timeout.total_seconds() > 0, s
        assert spec.max_tries > 0, s
        assert spec.on_deadline in spec.allowed and ActorType.WATCHDOG in spec.allowed[spec.on_deadline], s
        assert spec.on_exhausted in spec.allowed, s
        assert spec.purpose, s


def test_terminal_outcomes_are_exactly_the_mission_set():
    assert {t.value for t in sm.TERMINAL} == {"AUTO_REPAIRED", "AUTO_ROLLED_BACK", "SAFELY_ESCALATED", "CANNOT_REPRODUCE", "DUPLICATE_RESOLVED", "POLICY_REFUSED"}


def test_terminal_states_admit_no_machine_transition():
    for t in sm.TERMINAL:
        for dst in S:
            for actor in ActorType:
                if t is S.SAFELY_ESCALATED and actor is ActorType.OWNER and dst in (S.READY_FOR_RELEASE, S.PROMOTING):
                    continue
                try:
                    sm.check_transition(t, dst, actor, resumable=True)
                except sm.IllegalTransition:
                    continue
                raise AssertionError(f"{t.value} -> {dst.value} allowed for {actor.value}")


def test_only_the_owner_resumes_and_only_a_resumable_escalation():
    sm.check_transition(S.SAFELY_ESCALATED, S.READY_FOR_RELEASE, ActorType.OWNER, resumable=True)
    for actor in (ActorType.CONTROLLER, ActorType.WATCHDOG, ActorType.ACTIVITY, ActorType.RELEASE):
        try:
            sm.check_transition(S.SAFELY_ESCALATED, S.READY_FOR_RELEASE, actor, resumable=True)
            raise AssertionError(actor)
        except sm.IllegalTransition:
            pass
    try:
        sm.check_transition(S.SAFELY_ESCALATED, S.READY_FOR_RELEASE, ActorType.OWNER, resumable=False)
        raise AssertionError("a final escalation was resumed")
    except sm.IllegalTransition:
        pass


def test_a_model_activity_can_never_release_or_declare_recovery():
    for src in sm.NON_TERMINAL:
        for dst in (S.READY_FOR_RELEASE, S.RELEASING, S.AUTO_REPAIRED, S.AUTO_ROLLED_BACK, S.CANNOT_REPRODUCE):
            assert ActorType.ACTIVITY not in sm.spec(src).allowed.get(dst, frozenset()), (src, dst)


def test_the_documentation_table_is_generated_from_the_code():
    import pathlib

    doc = (pathlib.Path(__file__).resolve().parents[3] / "docs" / "MAINTENANCE_STATE_MACHINE.md").read_text(encoding="utf-8")
    for s in sm.NON_TERMINAL:
        spec = sm.spec(s)
        assert f"| `{s.value}` |" in doc, s
        assert f"on deadline -> `{spec.on_deadline.value}`" in doc, s


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow], database=None)
@given(st.lists(st.tuples(st.sampled_from(list(S)), st.sampled_from(list(ActorType)), st.booleans()), min_size=1, max_size=60))
def test_property_random_action_sequences_never_break_the_invariants(actions):
    """Generated (target, actor, resumable) attempts from DETECTED: every
    accepted move is in the table, no terminal state is left except by the
    owner resume edge, and every visited non-terminal state has a deadline."""
    state = S.DETECTED
    resumable = False
    for dst, actor, flag in actions:
        try:
            sm.check_transition(state, dst, actor, resumable=resumable)
        except sm.IllegalTransition:
            continue
        assert actor in sm.spec(state).allowed[dst]
        if state in sm.TERMINAL:
            assert state is S.SAFELY_ESCALATED and actor is ActorType.OWNER and resumable
        state = dst
        resumable = flag if state is S.SAFELY_ESCALATED else False
        if state not in sm.TERMINAL:
            assert sm.deadline_for(state, __import__("datetime").datetime(2026, 1, 1)) is not None
