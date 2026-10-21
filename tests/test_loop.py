"""Loop tests: the production behaviors — terminal states, budgets, retries."""

from __future__ import annotations

import json

import pytest

from triage_agent.loop import run
from triage_agent.providers import (
    MockProvider,
    ProviderError,
    final_response,
    tool_call_response,
)
from triage_agent.types import Budget, LLMResponse

VALID_REPORT = {
    "severity": "SEV2",
    "summary": "Checkout error rate spiked to 35% after the 13:58 deploy of v2.4.1.",
    "root_cause": "Deploy v2.4.1 regressed the payments client.",
    "confidence": 0.82,
    "evidence": [
        {"source": "list_deploys", "detail": "v2.4.1 at 13:58"},
        {"source": "get_metric", "detail": "error_rate_pct 0.4 -> 35.0"},
    ],
    "suggested_fix": "Roll back to v2.4.0 and investigate the payments client change.",
    "suspected_deploy": "v2.4.1",
}


@pytest.fixture
def settings():
    from triage_agent.config import Settings

    return Settings(provider="mock", model="mock-model", victim_root="victim")


def test_happy_path_produces_valid_report(settings) -> None:
    provider = MockProvider(
        [
            tool_call_response("list_deploys", {"limit": 5}, "c1"),
            tool_call_response("get_metric", {"name": "error_rate_pct"}, "c2"),
            final_response(json.dumps(VALID_REPORT)),
        ]
    )
    result = run("Investigate the incident.", provider=provider, settings=settings)
    assert result.status == "ok"
    assert result.report is not None
    assert result.report.severity == "SEV2"
    assert result.budget.steps_used == 3


def test_step_budget_exhaustion_is_graceful(settings) -> None:
    # model loops forever asking for deploys; budget must stop it, not hang
    provider = MockProvider([tool_call_response("list_deploys", {}, f"c{i}") for i in range(50)])
    result = run(
        "Investigate.",
        provider=provider,
        settings=settings,
        budget=Budget(max_steps=4),
    )
    assert result.status == "budget_exhausted"
    assert "step limit" in (result.reason or "")
    assert result.budget.steps_used == 4


def test_cost_budget_exhaustion(settings) -> None:
    provider = MockProvider([tool_call_response("list_deploys", {}, f"c{i}") for i in range(50)])
    result = run(
        "Investigate.",
        provider=provider,
        settings=settings,
        budget=Budget(max_steps=100, max_cost_usd=0.0),
    )
    assert result.status == "budget_exhausted"


def test_invalid_output_is_retried_with_feedback(settings) -> None:
    provider = MockProvider(
        [
            final_response("I think it was the deploy, probably."),  # not JSON
            final_response(json.dumps(VALID_REPORT)),  # corrected
        ]
    )
    result = run("Investigate.", provider=provider, settings=settings)
    assert result.status == "ok"
    # the retry prompt must have been appended to the conversation
    retry_seen = any(
        m.role == "user" and m.content and "Required shape" in m.content
        for m in result.messages
    )
    assert retry_seen


def test_invalid_output_gives_up_after_bounded_retries(settings) -> None:
    provider = MockProvider([final_response("still not json") for _ in range(6)])
    result = run("Investigate.", provider=provider, settings=settings)
    assert result.status == "invalid_output"


def test_tool_error_does_not_abort_run(settings) -> None:
    """A typo'd file path must be recoverable, not fatal."""
    provider = MockProvider(
        [
            tool_call_response("read_file", {"path": "does/not/exist"}, "c1"),
            final_response(json.dumps(VALID_REPORT)),
        ]
    )
    result = run("Investigate.", provider=provider, settings=settings)
    assert result.status == "ok"
    assert any(e.kind == "tool_call" and not e.data["ok"] for e in result.trace.events)


def test_provider_error_is_reported(settings) -> None:
    class Boom(MockProvider):
        def complete(self, *a, **k) -> LLMResponse:
            raise ProviderError("429 rate limited")

    result = run(
        "Investigate.",
        provider=Boom(),
        settings=settings,
        budget=Budget(llm_retries=0),
    )
    assert result.status == "provider_error"


def test_retry_backoff_recovers(settings, monkeypatch) -> None:
    """Transient failure on the first call, success on the second."""
    monkeypatch.setattr("triage_agent.providers.time.sleep", lambda *_: None)

    class Flaky(MockProvider):
        def __init__(self) -> None:
            super().__init__([final_response(json.dumps(VALID_REPORT))])
            self.n = 0

        def complete(self, *a, **k) -> LLMResponse:
            self.n += 1
            if self.n == 1:
                raise ProviderError("timeout")
            return super().complete(*a, **k)

    result = run("Investigate.", provider=Flaky(), settings=settings)
    assert result.status == "ok"
    assert result.budget.steps_used == 1  # the retry did not burn a step


def test_trace_records_cost_and_tokens(settings) -> None:
    provider = MockProvider([final_response(json.dumps(VALID_REPORT))])
    result = run("Investigate.", provider=provider, settings=settings)
    summary = result.trace.summary()
    assert summary["steps"] == 1
    assert summary["input_tokens"] == 200
    assert summary["cost_usd"] == 0.0  # mock is priced at zero
