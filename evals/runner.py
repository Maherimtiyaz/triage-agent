"""Eval harness — the thing that makes this a system, not a demo.

An eval is three parts:
  1. a DATASET of cases, each with a scenario and the ground truth
  2. a SCORER that turns a run into a number
  3. a GATE that fails CI when the number drops

Because this harness has a pure, injectable `provider`, evals run against a
recorded cassette — deterministic, free, and fast enough for every commit.
When you change the prompt or a tool description, the cassette goes stale and
the eval tells you exactly which scenarios regressed. THAT is the feedback loop
that lets you iterate on prompts safely.

Scoring here is deliberately simple (keyword coverage over `root_cause`). In
production you would enrich it: LLM-as-judge for nuance, plus hard assertions
on evidence quality and step count. Start simple — a scorer you trust beats a
clever one you don't.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Allow `python evals/runner.py` without an editable install.
REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "src", REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from triage_agent.config import Settings  # noqa: E402
from triage_agent.loop import run  # noqa: E402
from triage_agent.providers import MockProvider  # noqa: E402

FIXTURES = REPO / "victim" / "fixtures"


@dataclass
class Case:
    name: str
    fixture: str
    task: str
    cassette: str
    # any of these keyword groups must appear in root_cause/summary (lowercased)
    must_match: list[str]
    max_steps: int = 12
    max_cost_usd: float = 0.25


@dataclass
class Score:
    case: str
    status: str
    passed: bool
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    steps: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


# The golden dataset. One row per incident you have ground truth for.
DATASET: list[Case] = [
    Case(
        name="bad_deploy",
        fixture="bad_deploy.json",
        task="Alert: checkout error rate spiked to 35% at 14:05. Investigate.",
        cassette="bad_deploy.json",
        must_match=["deploy", "payments"],
    ),
    Case(
        name="slow_query",
        fixture="slow_query.json",
        task="Alert: p99 latency 2400ms at 11:55. Investigate.",
        cassette="slow_query.json",
        must_match=["connection", "pool"],
    ),
    Case(
        name="disk_full",
        fixture="disk_full.json",
        task="Alert: ingest halted with write failures at 02:16. Investigate.",
        cassette="disk_full.json",
        must_match=["disk"],
    ),
]


def score_case(case: Case, cassette_dir: Path) -> Score:
    """Run one case in an isolated sandbox and score the output."""
    with tempfile.TemporaryDirectory() as tmp:
        # isolate: the agent sees ONLY this scenario's data
        shutil.copy(FIXTURES / case.fixture, Path(tmp) / "state.json")
        settings = Settings(
            provider="mock",
            model="mock-model",
            victim_root=tmp,
            max_steps=case.max_steps,
        )
        provider = MockProvider.from_cassette(cassette_dir / case.cassette)

        from triage_agent.types import Budget

        result = run(
            case.task,
            provider=provider,
            settings=settings,
            budget=Budget(max_steps=case.max_steps, max_cost_usd=case.max_cost_usd),
        )

    sc = Score(case=case.name, status=result.status, passed=False)
    if result.budget:
        sc.steps = result.budget.steps_used
        sc.cost_usd = round(result.budget.cost_usd, 6)

    if result.status != "ok" or result.report is None:
        sc.notes.append(f"run did not produce a report ({result.status}: {result.reason})")
        return sc

    haystack = f"{result.report.root_cause} {result.report.summary}".lower()
    for kw in case.must_match:
        (sc.matched if kw.lower() in haystack else sc.missing).append(kw)

    # guardrail assertion: every evidence item must cite a real tool
    allowed_sources = {"query_logs", "list_deploys", "get_metric", "read_file"}
    if not result.report.evidence:
        sc.notes.append("no evidence cited")
    else:
        sources = {e.source for e in result.report.evidence}
        fabricated = sources - allowed_sources
        if fabricated:
            sc.notes.append(f"fabricated evidence sources: {sorted(fabricated)}")

    sc.passed = not sc.missing and not sc.notes
    return sc


def run_all(cassette_dir: Path | None = None) -> list[Score]:
    cassette_dir = cassette_dir or (Path(__file__).resolve().parent / "cassettes")
    return [score_case(c, cassette_dir) for c in DATASET]


def main() -> int:
    scores = run_all()
    total = len(scores)
    passed = sum(s.passed for s in scores)
    print(f"{'case':<12} {'status':<18} {'pass':<5} steps  cost     missing")
    print("-" * 72)
    for s in scores:
        print(
            f"{s.case:<12} {s.status:<18} {str(s.passed):<5} "
            f"{s.steps:<6} ${s.cost_usd:<7.4f} {','.join(s.missing) or '-'}"
        )
        for n in s.notes:
            print(f"    note: {n}")
    print("-" * 72)
    print(f"PASSED {passed}/{total}  ({passed / total:.0%})")
    # The gate: below 100% is a regression.
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
