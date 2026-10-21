"""Tool-layer tests: failures are returned, results are truncated."""

from __future__ import annotations

from triage_agent.guardrails import Policy
from triage_agent.tools import ALL_SPECS, build_registry, truncate_result
from triage_agent.types import ToolCall


def test_unknown_tool_is_returned_not_raised(policy: Policy) -> None:
    reg = build_registry(policy)
    out = reg.dispatch(ToolCall("c1", "does_not_exist", {}))
    assert out.ok is False
    assert "no tool named" in out.content


def test_missing_file_is_returned_not_raised(policy: Policy) -> None:
    reg = build_registry(policy)
    out = reg.dispatch(ToolCall("c1", "read_file", {"path": "nope.txt"}))
    assert out.ok is False
    assert out.error == "tool_exception"
    assert "ERROR" in out.content


def test_bad_arguments_are_caught(policy: Policy) -> None:
    reg = build_registry(policy)
    out = reg.dispatch(ToolCall("c1", "get_metric", {"wrong_arg": 1}))
    assert out.ok is False
    assert out.error == "bad_arguments"


def test_successful_tool_hits_floor(policy: Policy) -> None:
    reg = build_registry(policy)
    out = reg.dispatch(ToolCall("c1", "list_deploys", {"limit": 5}))
    assert out.ok is True
    assert "v2.4.1" in out.content


def test_truncate_keeps_head_and_tail() -> None:
    text = "START" + ("x" * 10_000) + "END"
    out, truncated = truncate_result(text, 400)
    assert truncated is True
    assert out.startswith("START")
    assert out.endswith("END")
    assert "omitted" in out


def test_truncate_noop_when_small() -> None:
    out, truncated = truncate_result("short", 400)
    assert out == "short" and truncated is False


def test_specs_hide_dangerous_tool_when_disabled(policy: Policy) -> None:
    reg = build_registry(policy)
    names = {s.name for s in reg.specs()}
    assert "run_cmd" not in names
    assert "run_cmd" in ALL_SPECS
