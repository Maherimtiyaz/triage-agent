"""CLI: run one triage investigation and print a trace + report.

    python -m triage_agent.cli "checkout latency spiked at 14:05"

With TRIAGE_PROVIDER=anthropic it calls a real model. Default is `mock`, which
runs a recorded script so you can see the harness work with no API key.
"""

from __future__ import annotations

import json
import sys

from triage_agent.config import load_settings
from triage_agent.loop import run
from triage_agent.providers import MockProvider, final_response, tool_call_response
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


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    scenario: str | None = None
    if "--scenario" in argv:
        i = argv.index("--scenario")
        try:
            scenario = argv[i + 1]
        except IndexError:
            raise SystemExit("--scenario requires a value") from None
        del argv[i : i + 2]

    task = " ".join(argv) if argv else "Investigate the incident: error rate spiked at 14:05."
    settings = load_settings()
    if scenario:
        settings.victim_root = _scenario_root(scenario)

    provider = None
    if settings.provider == "mock":
        provider = MockProvider(DEMO_SCRIPT)

    result = run(task, provider=provider, settings=settings, budget=Budget(max_steps=12))

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

    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
