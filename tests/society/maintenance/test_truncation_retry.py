"""A reply cut at the token limit is discarded and retried once with a
larger, capped token limit; a second cut reply ends the try."""

import asyncio
from decimal import Decimal

from app.maintenance.activities import SPECS, TRUNCATION_RETRY_MAX_TOKENS, ModelReply, run_activity

BUILDER = next(s for s in SPECS.values() if s.role == "builder")
CUT = '{"action": "apply_patch", "args": {"files": [{"path": "x.py", "operations": [{"op": "create", "text": "CUT_MARKER'


class FakeModel:
    provider, model_name, live = "fake", "fake-1", False

    def __init__(self, replies):
        self.replies, self.caps, self.calls = list(replies), [], []

    async def complete(self, messages, *, max_tokens, temperature=0.1):
        self.caps.append(max_tokens)
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


def _cut():
    return ModelReply(CUT, 10, 10, Decimal("0"), "length")


def _run(model, applied):
    tools = {"apply_patch": lambda args: applied.append(args) or {"applied": True}}
    return asyncio.run(run_activity(BUILDER, {"task": "t"}, model=model, tools=tools))


def test_truncated_reply_is_discarded_and_retried_once_with_capped_limit():
    ok = ModelReply('{"action": "submit", "result": {"summary": "done here"}}', 10, 10, Decimal("0"), "stop")
    model, applied = FakeModel([_cut(), ok]), []
    res = _run(model, applied)
    assert res.ok and res.output == {"summary": "done here"}
    assert len(model.caps) == 2  # exactly one retry
    assert model.caps[1] == min(2 * BUILDER.max_tokens, TRUNCATION_RETRY_MAX_TOKENS)
    assert applied == []  # the cut reply was never applied
    assert not any("CUT_MARKER" in m["content"] for m in model.calls[1])  # never re-sent
    assert not any("CUT_MARKER" in str(entry) for entry in res.turn_log)


def test_second_truncated_reply_ends_with_truncated_output():
    model, applied = FakeModel([_cut(), _cut()]), []
    res = _run(model, applied)
    assert not res.ok
    assert res.error_class == "truncated_output"
    assert applied == []
    assert len(model.caps) == 2
