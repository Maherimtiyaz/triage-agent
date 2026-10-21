"""The vendor seam.

Only this module is allowed to know that `anthropic` exists. Everything else
talks `types.Message` in / `types.LLMResponse` out. Swap vendors by adding a
class here — no other file changes.

The MockProvider is not a toy: it is how you get a DETERMINISTIC test suite for
a non-deterministic system. Recording real responses into cassettes and
replaying them in CI is the standard way to keep agent tests fast and free.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from triage_agent.types import LLMResponse, Message, StopReason, ToolCall, ToolSpec, Usage


class Provider(Protocol):
    """Everything the loop needs from a model vendor."""

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 2048,
        timeout_s: float = 60.0,
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------- #
# Mock / record-replay
# --------------------------------------------------------------------------- #


class ProviderError(RuntimeError):
    """Retryable-by-default provider failure (rate limit, 5xx, timeout)."""


class MockProvider:
    """A scripted provider.

    Two modes:
      * `script=[...]`    — return canned responses in order (unit tests)
      * `from_cassette()` — replay a recorded JSON transcript (evals, CI)
    """

    def __init__(self, script: list[LLMResponse] | None = None) -> None:
        self._script = list(script or [])
        self._i = 0
        self.calls: list[list[Message]] = []  # for assertions

    @classmethod
    def from_cassette(cls, path: str | Path) -> MockProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([_response_from_dict(r) for r in data["responses"]])

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 2048,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self._i >= len(self._script):
            # Ran out of script: terminate cleanly instead of hanging the loop.
            return LLMResponse(
                message=Message.assistant("(mock: script exhausted)"),
                stop_reason="end_turn",
            )
        resp = self._script[self._i]
        self._i += 1
        return resp


def tool_call_response(name: str, args: dict, call_id: str = "call_1") -> LLMResponse:
    """Test helper: a response that requests one tool."""
    return LLMResponse(
        message=Message.assistant(None, [ToolCall(id=call_id, name=name, arguments=args)]),
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=20),
    )


def final_response(text: str) -> LLMResponse:
    return LLMResponse(
        message=Message.assistant(text),
        stop_reason="end_turn",
        usage=Usage(input_tokens=200, output_tokens=80),
    )


def _response_from_dict(d: dict) -> LLMResponse:
    msg = d["message"]
    return LLMResponse(
        message=Message(
            role=msg.get("role", "assistant"),
            content=msg.get("content"),
            tool_calls=[ToolCall(**c) for c in msg.get("tool_calls", [])],
        ),
        stop_reason=d.get("stop_reason", "end_turn"),
        usage=Usage(**d.get("usage", {})),
    )


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


class AnthropicProvider:
    """Adapter for the Anthropic Messages API.

    Production details that matter and are easy to get wrong:
      * tool results MUST be carried in a `user` turn as `tool_result` blocks
      * one assistant turn may contain several `tool_use` blocks
      * `stop_reason` values differ per vendor — normalize them here
    """

    def __init__(self, api_key: str, model: str) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError("pip install 'triage-agent[anthropic]'") from exc
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 2048,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        system, wire = _to_anthropic_messages(messages)
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=wire,
                tools=[
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                    }
                    for t in tools
                ]
                or [],
                timeout=timeout_s,
            )
        except Exception as exc:  # normalize to one retryable type
            raise ProviderError(str(exc)) from exc

        return _from_anthropic_response(resp)


def _to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    wire: list[dict] = []

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content or "")
        elif m.role == "user":
            wire.append({"role": "user", "content": m.content or ""})
        elif m.role == "assistant":
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for c in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                )
            wire.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            wire.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content or "",
                        }
                    ],
                }
            )
    return "\n\n".join(system_parts), wire


def _from_anthropic_response(resp: object) -> LLMResponse:
    text_bits: list[str] = []
    calls: list[ToolCall] = []
    for block in getattr(resp, "content", []):
        btype = getattr(block, "type", None)
        if btype == "text":
            text_bits.append(getattr(block, "text", ""))
        elif btype == "tool_use":
            calls.append(
                ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
            )

    stop = getattr(resp, "stop_reason", "end_turn")
    normalized: StopReason = "tool_use" if calls else "end_turn"
    if stop == "max_tokens":
        normalized = "max_tokens"

    u = getattr(resp, "usage", None)
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) if u else 0,
        output_tokens=getattr(u, "output_tokens", 0) if u else 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) if u else 0,
    )
    return LLMResponse(
        message=Message.assistant("\n".join(text_bits) or None, calls),
        stop_reason=normalized,
        usage=usage,
    )


# --------------------------------------------------------------------------- #
# Retry wrapper
# --------------------------------------------------------------------------- #


def call_with_retry(
    provider: Provider,
    messages: list[Message],
    tools: list[ToolSpec],
    *,
    max_tokens: int,
    timeout_s: float,
    retries: int,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> LLMResponse:
    """Exponential backoff + jitter around any provider call.

    This is production lesson: a transient 429 at step 19 of a 20-step run must
    not destroy the whole run. Jitter prevents synchronized retry stampedes.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return provider.complete(
                messages, tools, max_tokens=max_tokens, timeout_s=timeout_s
            )
        except ProviderError as exc:
            last = exc
            if attempt == retries:
                break
            delay = min(2**attempt, 8) * (0.5 + random.random() / 2)
            if on_retry:
                on_retry(attempt + 1, delay, exc)
            time.sleep(delay)
    raise ProviderError(f"provider failed after {retries + 1} attempts: {last}")
