"""The agent loop. This is the whole harness in one place.

Read this file top to bottom and you have the production pattern:

    bootstrap -> [budget check -> compact -> model -> tools -> observe]* -> finalize

Every `# M<n>` marker maps to a milestone in the README so you can see which
production lesson each block encodes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from triage_agent.config import MODEL_PRICING, Settings, load_settings
from triage_agent.context import ContextPolicy, compact_if_needed
from triage_agent.guardrails import Policy, account_usage
from triage_agent.output import IncidentReport, parse_report, retry_message
from triage_agent.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAICompatibleProvider,
    Provider,
    ProviderError,
    call_with_retry,
)
from triage_agent.tools import build_registry
from triage_agent.tracing import Trace
from triage_agent.types import Budget, Message

Status = Literal["ok", "budget_exhausted", "provider_error", "invalid_output"]

SYSTEM_PROMPT = """You are an SRE incident triage agent.

You investigate a production incident using ONLY the tools provided. You never
guess: every claim in your report must cite a tool result you actually saw.

Method:
1. Establish the timeline — what changed recently (deploys) and when symptoms began.
2. Look for the symptom in logs and metrics; narrow with specific filters.
3. Form a hypothesis, then try to FALSIFY it with another query before settling.
4. When confident, stop investigating and emit your report.

Budget discipline: you have a limited number of investigation steps. Prefer
two targeted queries over ten broad ones. Do not repeat a query you have
already run — if you have the answer, stop.

When you are done, respond with ONLY a JSON object (no prose, no code fence
needed) shaped like:
{
  "severity": "SEV1" | "SEV2" | "SEV3" | "SEV4",
  "summary": "<one sentence, what is broken and who is affected>",
  "root_cause": "<the single most likely cause>",
  "confidence": <0.0-1.0>,
  "evidence": [{"source": "<tool>", "detail": "<what you saw>"}],
  "suggested_fix": "<the concrete next action>",
  "suspected_deploy": "<version or null>"
}

CRITICAL for `evidence`: each item's `source` MUST be the exact name of a tool
you called — one of query_logs, list_deploys, get_metric, read_file. Never cite
"alert", "logs", "metrics" or any other label, and never cite a tool you did
not actually call. If you have no tool evidence, say so and set confidence low.
"""


@dataclass
class AgentResult:
    status: Status
    report: IncidentReport | None = None
    raw_output: str | None = None
    reason: str | None = None
    trace: Trace | None = None
    budget: Budget | None = None
    messages: list[Message] = field(default_factory=list)


# Any endpoint that speaks POST /v1/chat/completions. Ollama is the free path.
OPENAI_COMPATIBLE_ALIASES = frozenset(
    {"openai-compatible", "openai_compatible", "openai", "ollama", "omniroute", "lmstudio", "vllm"}
)


def build_provider(settings: Settings, script: list | None = None) -> Provider:
    kind = settings.provider.strip().lower()
    if kind == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key, settings.model)
    if kind in OPENAI_COMPATIBLE_ALIASES:
        return OpenAICompatibleProvider(settings.base_url, settings.model, settings.api_key)
    return MockProvider(script)


def run(
    task: str,
    *,
    provider: Provider | None = None,
    settings: Settings | None = None,
    budget: Budget | None = None,
    context_policy: ContextPolicy | None = None,
) -> AgentResult:
    settings = settings or load_settings()
    price = MODEL_PRICING.get(settings.model, MODEL_PRICING["mock-model"])
    budget = budget or Budget(max_steps=settings.max_steps, max_cost_usd=settings.max_cost_usd)
    context_policy = context_policy or ContextPolicy()

    # M1: deny-by-default policy + sandbox root
    policy = Policy(
        allowed_tools=settings.allowed_tools,
        allow_run_cmd=settings.allow_run_cmd,
        sandbox_root=_sandbox_root(settings),
    )
    registry = build_registry(policy)

    # M3: every run gets an id and a trace
    trace = Trace(run_id=f"run_{uuid.uuid4().hex[:8]}")
    messages: list[Message] = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message.user(task),
    ]
    trace.record("run_start", 0, task=task, model=settings.model,
                 allowed_tools=sorted(policy.allowed_tools), run_cmd=policy.allow_run_cmd)

    provider = provider or build_provider(settings)
    output_retries = 0

    while True:
        # ---- M4: budget gate (checked BEFORE spending more) ----------------
        if reason := budget.exhausted():
            trace.record("run_end", budget.steps_used, status="budget_exhausted", reason=reason)
            return AgentResult(
                status="budget_exhausted", reason=reason, trace=trace,
                budget=budget, messages=messages,
            )

        # ---- M6: context management on every turn --------------------------
        messages, did_compact = compact_if_needed(messages, context_policy)
        if did_compact:
            tokens = context_policy.token_estimate(messages)
            trace.record("compact", budget.steps_used, tokens=tokens)

        # ---- M4: model call, retried with backoff --------------------------
        specs = registry.specs()
        t0 = time.perf_counter()
        try:
            resp = call_with_retry(
                provider, messages, specs,
                max_tokens=2048, timeout_s=budget.llm_timeout_s,
                retries=budget.llm_retries,
                on_retry=lambda n, d, e: trace.record(
                    "llm_call", budget.steps_used, retry=n, delay=round(d, 2), error=str(e)[:200]
                ),
            )
        except ProviderError as exc:
            trace.record("run_end", budget.steps_used, status="provider_error", reason=str(exc))
            return AgentResult(
                status="provider_error", reason=str(exc), trace=trace,
                budget=budget, messages=messages,
            )
        latency_ms = (time.perf_counter() - t0) * 1000

        budget.steps_used += 1
        account_usage(budget, resp.usage.input_tokens, resp.usage.output_tokens, price)
        trace.llm(
            budget.steps_used,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
            latency_ms=latency_ms,
            cost_usd=(
                resp.usage.input_tokens * price["input"]
                + resp.usage.output_tokens * price["output"]
            ) / 1_000_000,
            tool_names=[c.name for c in resp.message.tool_calls],
        )
        messages.append(resp.message)

        # ---- M2: tool execution -------------------------------------------
        if resp.stop_reason == "tool_use" and resp.message.tool_calls:
            for call in resp.message.tool_calls:
                timeout = _spec_timeout(call.name, settings)
                tt0 = time.perf_counter()
                outcome = registry.dispatch_with_timeout(call, timeout)
                trace.tool(
                    budget.steps_used, name=call.name, ok=outcome.ok, error=outcome.error,
                    truncated=outcome.truncated,
                    latency_ms=(time.perf_counter() - tt0) * 1000,
                    args=call.arguments,
                )
                messages.append(Message.tool_result(call.id, outcome.content))
            continue

        # ---- M2: terminal path — validate structured output ---------------
        text = resp.message.content or ""
        report, error = parse_report(text)
        if report is not None:
            trace.record("run_end", budget.steps_used, status="ok", severity=report.severity)
            return AgentResult(
                status="ok", report=report, raw_output=text, trace=trace,
                budget=budget, messages=messages,
            )

        # invalid output: retry with the error fed back, bounded (M2 + M4)
        output_retries += 1
        trace.record(
            "output_retry", budget.steps_used, attempt=output_retries, error=(error or "")[:200]
        )
        if output_retries > 2:
            trace.record("run_end", budget.steps_used, status="invalid_output")
            return AgentResult(
                status="invalid_output", raw_output=text, reason=error, trace=trace,
                budget=budget, messages=messages,
            )
        messages.append(Message.user(retry_message(error or "unknown")))


def _sandbox_root(settings: Settings) -> Path:
    """Resolve the directory the agent is allowed to read.

    Resolution order:
      1. absolute path -> use as-is
      2. relative path that exists -> relative to the CURRENT WORKING DIRECTORY
      3. otherwise -> relative to the repo root (``src/../..``)

    This makes the agent work both when you run it from a git clone and when
    the package is installed, without hard-coding a machine-specific path.
    """
    root = Path(settings.victim_root)
    if root.is_absolute():
        return root
    if root.exists():
        return root.resolve()
    return Path(__file__).resolve().parents[2] / root


def _spec_timeout(name: str, settings: Settings) -> float:
    from triage_agent.tools import ALL_SPECS

    spec = ALL_SPECS.get(name)
    return spec.timeout_s if spec else settings.tool_timeout_s


__all__ = ["AgentResult", "run", "build_provider", "SYSTEM_PROMPT"]
