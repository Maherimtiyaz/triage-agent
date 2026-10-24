"""CLI: run one triage investigation and print a trace + report.

    python -m triage_agent.cli "checkout latency spiked at 14:05"

With TRIAGE_PROVIDER=anthropic it calls a real model. Default is `mock`, which
runs a recorded script so you can see the harness work with no API key.
"""

from __future__ import annotations

import json
import sys

from triage_agent.config import Settings, load_settings
from triage_agent.loop import build_provider, run
from triage_agent.providers import (
    MockProvider,
    RecordingProvider,
    final_response,
    tool_call_response,
)
from triage_agent.types import Budget

DEMO_SCRIPT = [
    tool_call_response("list_deploys", {"limit": 5}, "c1"),       # when did it start?
    tool_call_response("query_logs", {"level": "ERROR", "limit": 20}, "c2"),
    tool_call_response("get_metric", {"name": "error_rate_pct"}, "c3"),
    final_response(
        json.dumps(
            {
                "severity": "SEV2",
                "summary": "Checkout began failing after the 13:58 deploy, "
                "affecting ~35% of traffic.",
                "root_cause": "Deploy v2.4.1 regressed the payments client, "
                "causing a 35% error rate.",
                "confidence": 0.82,
                "evidence": [
                    {
                        "source": "list_deploys",
                        "detail": "v2.4.1 deployed at 13:58, minutes before onset",
                    },
                    {
                        "source": "query_logs",
                        "detail": "ERROR 'upstream 500 from payments' after 14:00",
                    },
                    {
                        "source": "get_metric",
                        "detail": "error_rate_pct rose from 0.4 to 35.0",
                    },
                ],
                "suggested_fix": "Roll back to v2.4.0 and investigate the "
                "payments client change.",
                "suspected_deploy": "v2.4.1",
            }
        )
    ),
]


def _scenario_root(scenario: str) -> str:
    """Materialize one fixture as an isolated sandbox root.

    The agent should see exactly one incident's data — never the union of all
    fixtures. Same trick the eval runner uses.
    """
    import shutil
    import tempfile
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    fixture = repo / "victim" / "fixtures" / f"{scenario}.json"
    if not fixture.is_file():
        available = sorted(p.stem for p in (repo / "victim" / "fixtures").glob("*.json"))
        raise SystemExit(f"unknown scenario '{scenario}'. Available: {available}")
    tmp = Path(tempfile.mkdtemp(prefix="triage-"))
    shutil.copy(fixture, tmp / "state.json")
    return str(tmp)


def _take_value(argv: list[str], flag: str) -> str | None:
    """Pop `--flag VALUE` out of argv. Returns None when the flag is absent."""
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 >= len(argv):
        raise SystemExit(f"{flag} requires a value")
    value = argv[i + 1]
    del argv[i : i + 2]
    return value


def _replay_matches(
    cassette: str, task: str, settings: Settings, expected: tuple[str, int]
) -> bool:
    """Re-run the task from the cassette; compare status and step count.

    A cassette that does not replay identically is worse than no cassette: it
    silently changes what CI asserts. So recording verifies itself immediately.
    """
    replay = MockProvider.from_cassette(cassette)
    budget = Budget(max_steps=settings.max_steps, max_cost_usd=settings.max_cost_usd)
    result = run(task, provider=replay, settings=settings, budget=budget)
    steps = result.budget.steps_used if result.budget else 0
    return (result.status, steps) == expected


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    scenario = _take_value(argv, "--scenario")
    record_to = _take_value(argv, "--record")
    replay_from = _take_value(argv, "--replay")
    if record_to and replay_from:
        raise SystemExit("--record and --replay are mutually exclusive")

    task = " ".join(argv) if argv else "Investigate the incident: error rate spiked at 14:05."
    settings = load_settings()
    if scenario:
        settings.victim_root = _scenario_root(scenario)

    recorder: RecordingProvider | None = None
    if replay_from:
        provider = MockProvider.from_cassette(replay_from)
    elif record_to:
        if settings.provider.strip().lower() == "mock":
            raise SystemExit(
                "--record needs a real model. Set TRIAGE_PROVIDER=openai-compatible "
                "(free, via Ollama) or TRIAGE_PROVIDER=anthropic first."
            )
        recorder = RecordingProvider(
            build_provider(settings),
            record_to,
            metadata={
                "provider": settings.provider,
                "model": settings.model,
                "scenario": scenario,
                "task": task,
            },
        )
        provider = recorder
    elif settings.provider == "mock":
        provider = MockProvider(DEMO_SCRIPT)
    else:
        provider = None  # run() builds the real provider

    budget = Budget(max_steps=settings.max_steps, max_cost_usd=settings.max_cost_usd)
    result = run(task, provider=provider, settings=settings, budget=budget)

    print("=" * 72)
    print("TRACE")
    print("=" * 72)
    if result.trace:
        print(result.trace.render())
        print("-" * 72)
        print(json.dumps(result.trace.summary(), indent=2))

    print("=" * 72)
    print(f"RESULT: {result.status}")
    print("=" * 72)
    if result.report:
        print(result.report.model_dump_json(indent=2))
    else:
        print(f"reason: {result.reason}")
        if result.raw_output:
            print(f"raw: {result.raw_output[:500]}")

    if recorder is not None:
        path = recorder.save()
        expected = (result.status, result.budget.steps_used if result.budget else 0)
        verified = _replay_matches(str(path), task, settings, expected)
        print("-" * 72)
        print(f"RECORDED  {len(recorder.responses)} model responses -> {path}")
        if verified:
            print("VERIFIED  cassette replays with identical status and step count")
        else:
            print("WARNING   replay diverged from the recorded run; do not trust this cassette")
            return 1

    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
