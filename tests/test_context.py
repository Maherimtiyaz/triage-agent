"""Context-management tests."""

from __future__ import annotations

from triage_agent.context import ContextPolicy, compact_if_needed
from triage_agent.types import Message, ToolCall


def _long_messages(n: int, chars: int = 5000) -> list[Message]:
    msgs = [Message(role="system", content="sys"), Message.user("investigate")]
    for i in range(n):
        msgs.append(Message.assistant(None, [ToolCall(f"c{i}", "query_logs", {})]))
        msgs.append(Message.tool_result(f"c{i}", "x" * chars))
    return msgs


def test_no_compaction_when_under_budget() -> None:
    policy = ContextPolicy(max_context_tokens=1_000_000)
    msgs = _long_messages(3)
    out, compacted = compact_if_needed(msgs, policy)
    assert compacted is False
    assert out is msgs


def test_compaction_triggers_and_preserves_head_and_tail() -> None:
    policy = ContextPolicy(max_context_tokens=2_000, keep_recent_messages=4)
    msgs = _long_messages(20)
    out, compacted = compact_if_needed(msgs, policy)
    assert compacted is True
    assert out[0] is msgs[0]  # system preserved
    assert out[1] is msgs[1]  # task preserved
    assert out[-1] is msgs[-1]  # most recent preserved
    assert len(out) < len(msgs)
    # the model is TOLD that eviction happened (see module docstring)
    assert any(m.content and "compacted" in m.content for m in out)


def test_compaction_reports_prior_actions() -> None:
    policy = ContextPolicy(max_context_tokens=2_000, keep_recent_messages=2)
    msgs = _long_messages(20)
    out, _ = compact_if_needed(msgs, policy)
    digest = next(m.content for m in out if m.content and "compacted" in m.content)
    assert "query_logs" in digest


def test_compaction_is_idempotent_under_pressure() -> None:
    policy = ContextPolicy(max_context_tokens=2_000, keep_recent_messages=4)
    msgs = _long_messages(20)
    once, _ = compact_if_needed(msgs, policy)
    twice, compacted_again = compact_if_needed(once, policy)
    assert compacted_again is True
    assert twice[0] is once[0]
