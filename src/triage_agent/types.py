"""Provider-agnostic wire types.

WHY this file exists: every harness starts by picking ONE model vendor and ends
up rewriting everything when it changes. These dataclasses are the seam. Only
`providers.py` knows about a vendor SDK; everything else speaks this dialect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["tool_use", "end_turn", "max_tokens"]


@dataclass(frozen=True)
class ToolSpec:
    """What we show the model. The schema IS the prompt — see tools.py."""

    name: str
    description: str
    input_schema: dict[str, Any]
    # dangerous tools never run unless explicitly allowlisted (guardrails.py)
    dangerous: bool = False
    # hard wall-clock cap for one invocation, seconds
    timeout_s: float = 10.0
    # max characters returned to the model — context is the scarcest resource
    max_result_chars: int = 4_000


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    @staticmethod
    def user(text: str) -> Message:
        return Message(role="user", content=text)

    @staticmethod
    def assistant(text: str, calls: list[ToolCall] | None = None) -> Message:
        return Message(role="assistant", content=text, tool_calls=calls or [])

    @staticmethod
    def tool_result(call_id: str, text: str) -> Message:
        return Message(role="tool", content=text, tool_call_id=call_id)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass
class LLMResponse:
    message: Message
    stop_reason: StopReason
    usage: Usage = field(default_factory=Usage)


@dataclass
class Budget:
    """Production lesson #1: every run is bounded along several axes at once.

    A single `max_steps` is not enough. A run can be step-bounded but still
    blow the cost ceiling, or stay cheap but spin forever. Track them apart.
    """

    max_steps: int = 25
    max_input_tokens: int = 300_000
    max_total_tokens: int = 500_000
    max_cost_usd: float = 1.0
    llm_timeout_s: float = 60.0
    llm_retries: int = 3

    # live counters (mutated by the loop)
    steps_used: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    output_retries_used: int = 0

    def exhausted(self) -> str | None:
        """Return the reason the run must stop, or None to continue.

        Budget exhaustion is a FIRST-CLASS RESULT, not an exception. Callers
        get `Result(status="budget_exhausted", reason=...)` back gracefully.
        """
        if self.steps_used >= self.max_steps:
            return f"step limit reached ({self.max_steps})"
        if self.tokens_used >= self.max_total_tokens:
            return f"token limit reached ({self.max_total_tokens})"
        if self.cost_usd >= self.max_cost_usd:
            return f"cost limit reached (${self.max_cost_usd:.2f})"
        return None
