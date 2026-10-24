"""OpenAI-compatible provider tests.

The wire-format conversions are pure functions, so they get real assertions.
The network path gets one test that proves failures surface as `ProviderError`
(and therefore are retryable) rather than escaping as raw urllib exceptions.
"""

from __future__ import annotations

import pytest

from triage_agent.providers import (
    OpenAICompatibleProvider,
    ProviderError,
    _from_openai_response,
    _to_openai_messages,
    _to_openai_tools,
)
from triage_agent.types import Message, ToolCall, ToolSpec


def test_tool_arguments_are_serialized_as_json_string() -> None:
    """OpenAI requires `arguments` to be a JSON string, not an object."""
    msgs = [Message.assistant(None, [ToolCall("c1", "query_logs", {"level": "ERROR"})])]
    wire = _to_openai_messages(msgs)
    assert wire[0]["tool_calls"][0]["function"]["arguments"] == '{"level": "ERROR"}'
    assert isinstance(wire[0]["tool_calls"][0]["function"]["arguments"], str)


def test_tool_result_becomes_role_tool() -> None:
    wire = _to_openai_messages([Message.tool_result("c1", "some logs")])
    assert wire[0] == {"role": "tool", "tool_call_id": "c1", "content": "some logs"}


def test_system_and_user_pass_through() -> None:
    wire = _to_openai_messages(
        [Message(role="system", content="sys"), Message.user("hi")]
    )
    assert wire == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_tools_are_wrapped_as_functions() -> None:
    spec = ToolSpec(
        name="get_metric",
        description="Fetch a metric",
        input_schema={"type": "object", "properties": {}},
    )
    wire = _to_openai_tools([spec])
    assert wire[0]["type"] == "function"
    assert wire[0]["function"]["name"] == "get_metric"
    assert wire[0]["function"]["parameters"] == spec.input_schema


def test_response_with_tool_calls_maps_to_tool_use() -> None:
    data = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "list_deploys",
                                "arguments": '{"limit": 5}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 78, "completion_tokens": 16},
    }
    resp = _from_openai_response(data)
    assert resp.stop_reason == "tool_use"
    assert resp.message.tool_calls[0].name == "list_deploys"
    assert resp.message.tool_calls[0].arguments == {"limit": 5}
    assert resp.usage.input_tokens == 78
    assert resp.usage.output_tokens == 16


def test_plain_text_response_maps_to_end_turn() -> None:
    data = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}
    resp = _from_openai_response(data)
    assert resp.stop_reason == "end_turn"
    assert resp.message.content == "done"


def test_length_finish_maps_to_max_tokens() -> None:
    data = {"choices": [{"finish_reason": "length", "message": {"content": "trunc"}}]}
    assert _from_openai_response(data).stop_reason == "max_tokens"


def test_invalid_json_arguments_do_not_raise() -> None:
    """Small local models sometimes emit malformed arguments; the tool layer
    must report 'bad arguments' instead of crashing the run."""
    data = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "get_metric", "arguments": "{oops"},
                        }
                    ]
                },
            }
        ]
    }
    resp = _from_openai_response(data)
    assert resp.message.tool_calls[0].arguments == {"__invalid_json": "{oops"}


def test_connection_failure_is_a_provider_error() -> None:
    """Must be retryable (ProviderError), not a raw URLError."""
    p = OpenAICompatibleProvider("http://127.0.0.1:9/v1", "model", "")
    with pytest.raises(ProviderError):
        p.complete([Message.user("hi")], [], timeout_s=1.0)
