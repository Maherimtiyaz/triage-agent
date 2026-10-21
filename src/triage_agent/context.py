"""Context management — the subsystem that separates toys from production.

A toy ignores context until the API returns `context_length_exceeded` and the
run dies. Production treats the context window as a scarce, monotonically
shrinking resource and manages it on every turn.

Two techniques, in order of preference:
  1. TRUNCATE at the source (tools.py already caps results).
  2. COMPACT history — summarize old turns into one paragraph, keep recent ones
     verbatim. Cheap, lossy, and almost always good enough.

What you must NOT do: silently drop messages. The model then "forgets" it
already checked the logs and re-checks them forever. If you evict, tell the
model in-band that you did.
"""

from __future__ import annotations

from dataclasses import dataclass

from triage_agent.types import Message

# Rough: ~4 chars per token. Good enough for budgeting; never trust it exactly.
CHARS_PER_TOKEN = 4


@dataclass
class ContextPolicy:
    max_context_tokens: int = 150_000
    # Always keep the system prompt + original task, plus this many recent turns.
    keep_recent_messages: int = 8
    compaction_notice: str = (
        "[context compacted: earlier tool results were summarized to fit the "
        "context window. Key findings retained below.]"
    )

    def token_estimate(self, messages: list[Message]) -> int:
        return sum(len(m.content or "") for m in messages) // CHARS_PER_TOKEN


def compact_if_needed(
    messages: list[Message],
    policy: ContextPolicy,
    *,
    summarize=None,  # optional Callable[[list[Message]], str]
) -> tuple[list[Message], bool]:
    """Return (possibly-compacted messages, did_compact).

    Strategy: freeze the head (system/user context) and the tail (recent work);
    collapse the middle into a single notice. If a `summarize` callable is
    supplied, use a model to write the middle-summary; otherwise use a cheap
    deterministic stub. Both are legitimate — start deterministic.
    """
    if policy.token_estimate(messages) <= policy.max_context_tokens:
        return messages, False

    if len(messages) <= policy.keep_recent_messages + 2:
        # Can't compact further without destroying the task. Let it through;
        # the provider will error and the budget layer will bail out cleanly.
        return messages, False

    head = messages[:2]  # system + original task (if present)
    tail = messages[-policy.keep_recent_messages :]
    middle = messages[2 : -policy.keep_recent_messages]

    digest = summarize(middle) if summarize is not None else _deterministic_digest(middle)

    notice = Message.user(f"{policy.compaction_notice}\n\n{digest}")
    return [*head, notice, *tail], True


def _deterministic_digest(messages: list[Message]) -> str:
    """No model call: just note which tools ran and whether they errored.

    Deterministic compaction is underrated — it never fails, never costs money,
    and for 'have I already tried this?' it is usually sufficient.
    """
    seen: list[str] = []
    for m in messages:
        for c in m.tool_calls:
            seen.append(f"- called {c.name}({_args(c.arguments)})")
        if m.role == "tool" and m.content and m.content.startswith(("ERROR", "DENIED")):
            seen.append(f"  -> {m.content.splitlines()[0][:120]}")
    return "Earlier actions:\n" + ("\n".join(seen) if seen else "(none)")


def _args(a: dict, n: int = 60) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in a.items())
    return s if len(s) <= n else s[:n] + "..."
