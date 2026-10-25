"""The vendor seam.

Only this module is allowed to know that `anthropic` exists. Everything else
talks `types.Message` in / `types.LLMResponse` out. Swap vendors by adding a
class here — no other file changes.

The MockProvider is not a toy: it is how you get a DETERMINISTIC test suite for
a non-deterministic system. Recording real responses into cassettes and
replaying them in CI is the standard way to keep agent tests fast and free.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
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


class CassetteMismatch(RuntimeError):
    """The request built at replay time is not the one that was recorded.

    Deliberately NOT a `ProviderError`: retrying cannot fix a stale cassette,
    and treating it as transient would hide exactly the drift this exists to
    catch. The loop turns it into a first-class `cassette_stale` status.
    """


def canonical_request_hash(messages: list[Message], tools: list[ToolSpec]) -> str:
    """Fingerprint everything that determines a model response.

    Covers the conversation (so TOOL OUTPUT changes count) and the tool specs
    (so schema and description changes count). Recording only the response side
    is what let a cassette replay happily after a tool started returning a
    different shape — the transcript looked fine, the world had moved on.
    """
    payload = {
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in m.tool_calls
                ],
                "tool_call_id": m.tool_call_id,
            }
            for m in messages
        ],
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class MockProvider:
    """A scripted provider.

    Two modes:
      * `script=[...]`    — return canned responses in order (unit tests)
      * `from_cassette()` — replay a recorded JSON transcript (evals, CI)

    Cassettes recorded with a `request_hash` are verified: if the harness now
    builds a different request than the one recorded, replay raises
    `CassetteMismatch` instead of answering a question nobody asked.
    """

    def __init__(
        self,
        script: list[LLMResponse] | None = None,
        *,
        request_hashes: list[str | None] | None = None,
        verify: bool = True,
        source: str | None = None,
    ) -> None:
        self._script = list(script or [])
        self._hashes = list(request_hashes or [])
        self._verify = verify
        self._source = source
        self._i = 0
        self.calls: list[list[Message]] = []  # for assertions

    @classmethod
    def from_cassette(cls, path: str | Path, *, verify: bool = True) -> MockProvider:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            [_response_from_dict(r) for r in data["responses"]],
            request_hashes=[r.get("request_hash") for r in data["responses"]],
            verify=verify,
            source=str(path),
        )

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

        expected = self._hashes[self._i] if self._i < len(self._hashes) else None
        if self._verify and expected:
            actual = canonical_request_hash(messages, tools)
            if actual != expected:
                raise CassetteMismatch(
                    f"cassette '{self._source}' is stale at turn {self._i + 1}: the "
                    "harness built a different request than the one recorded. A tool "
                    "output, a tool schema, or the system prompt changed since "
                    "recording. Re-record it: --record <cassette path>"
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


CASSETTE_VERSION = 1


def _response_to_dict(resp: LLMResponse, request_hash: str | None = None) -> dict:
    """Inverse of `_response_from_dict` — the on-disk cassette shape.

    `request_hash` is what makes replay verifiable: it binds this response to
    the exact request that produced it.
    """
    msg = resp.message
    entry: dict = {
        "message": {
            "role": msg.role,
            "content": msg.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in msg.tool_calls
            ],
        },
        "stop_reason": resp.stop_reason,
        "usage": {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read_tokens": resp.usage.cache_read_tokens,
        },
    }
    if request_hash:
        entry["request_hash"] = request_hash
    return entry


class RecordingProvider:
    """Wrap a real provider and persist its responses as a replayable cassette.

    The asymmetry this fixes: a live run costs money, needs a network, needs the
    vendor to be up, and is not reproducible. A cassette needs none of that and
    replays byte-identically. Recording once is what turns "I saw it work on my
    laptop" into "CI checks it on every commit".

    Only successful calls are recorded. A `ProviderError` that gets retried never
    lands in the cassette, so a replay cannot inherit a transient failure.
    """

    def __init__(
        self,
        inner: Provider,
        path: str | Path,
        metadata: dict | None = None,
    ) -> None:
        self._inner = inner
        self._path = Path(path)
        self._metadata = dict(metadata or {})
        self.responses: list[LLMResponse] = []
        self.requests: list[list[Message]] = []
        self.request_hashes: list[str] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 2048,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        self.requests.append(list(messages))
        # Hash BEFORE the call so the response is bound to the request that
        # actually produced it, not to whatever the harness built afterwards.
        request_hash = canonical_request_hash(messages, tools)
        resp = self._inner.complete(
            messages, tools, max_tokens=max_tokens, timeout_s=timeout_s
        )
        # Deliberately after the call: a raise means nothing is recorded.
        self.responses.append(resp)
        self.request_hashes.append(request_hash)
        return resp

    def save(self) -> Path:
        """Write the cassette in the exact shape `MockProvider.from_cassette` reads."""
        payload = {
            "version": CASSETTE_VERSION,
            "metadata": {
                **self._metadata,
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "turns": len(self.responses),
                "verifiable": True,
            },
            "responses": [
                _response_to_dict(r, self.request_hashes[i])
                for i, r in enumerate(self.responses)
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return self._path


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
# OpenAI-compatible endpoints (Ollama, OmniRoute, LM Studio, vLLM, OpenRouter)
# --------------------------------------------------------------------------- #


class OpenAICompatibleProvider:
    """Adapter for any endpoint that speaks `POST /v1/chat/completions`.

    This is the pragmatic default for free/self-hosted models: Ollama exposes
    it natively, and so do OmniRoute, LM Studio, llama.cpp, vLLM and OpenRouter.
    Written against stdlib urllib so it adds no dependency to the base install.

    Note the two format quirks vs Anthropic:
      * tool results are `role: "tool"` messages keyed by `tool_call_id`
      * `arguments` must be a JSON *string*, not an object
    """

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 2048,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        body: dict = {
            "model": self._model,
            "messages": _to_openai_messages(messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
            body["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ProviderError(f"HTTP {exc.code} from {self._url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"request to {self._url} failed: {exc}") from exc

        return _from_openai_response(data)


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    wire: list[dict] = []
    for m in messages:
        if m.role in ("system", "user"):
            wire.append({"role": m.role, "content": m.content or ""})
        elif m.role == "assistant":
            item: dict = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            # OpenAI requires a JSON *string*, not an object
                            "arguments": json.dumps(c.arguments),
                        },
                    }
                    for c in m.tool_calls
                ]
            wire.append(item)
        elif m.role == "tool":
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content or "",
                }
            )
    return wire


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _from_openai_response(data: dict) -> LLMResponse:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                # Small models sometimes emit invalid JSON. Surface it as data
                # so the tool layer reports "bad arguments" instead of crashing.
                arguments = {"__invalid_json": raw_args}
        else:
            arguments = dict(raw_args or {})
        calls.append(
            ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=arguments)
        )

    finish = choice.get("finish_reason")
    stop: StopReason = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")

    u = data.get("usage") or {}
    usage = Usage(
        input_tokens=u.get("prompt_tokens", 0),
        output_tokens=u.get("completion_tokens", 0),
        cache_read_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    )
    return LLMResponse(
        message=Message.assistant(msg.get("content"), calls),
        stop_reason=stop,
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
