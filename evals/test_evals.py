"""The eval gate, as a pytest test.

This is the file that runs in CI. It fails the build if the agent's accuracy
drops below the bar — which is what makes prompt changes safe to ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals.runner import DATASET, run_all

PASS_THRESHOLD = 1.0  # require every golden case to pass


@pytest.fixture(scope="module")
def scores():
    cassettes = Path(__file__).resolve().parent / "cassettes"
    return run_all(cassettes)


def test_dataset_is_non_empty() -> None:
    assert DATASET, "no eval cases defined"


def test_all_cases_produce_a_report(scores) -> None:
    bad = [s.case for s in scores if s.status != "ok"]
    assert not bad, f"cases failed to produce a report: {bad}"


def test_accuracy_meets_threshold(scores) -> None:
    passed = sum(s.passed for s in scores)
    rate = passed / len(scores)
    assert rate >= PASS_THRESHOLD, (
        f"accuracy {rate:.0%} below threshold {PASS_THRESHOLD:.0%}: "
        + ", ".join(f"{s.case}({'/'.join(s.missing) or s.notes})" for s in scores if not s.passed)
    )


def test_no_case_blows_its_step_budget(scores) -> None:
    """A correct answer reached in 40 steps is a failure with extra cost."""
    for s in scores:
        assert s.steps <= 12, f"{s.case} used {s.steps} steps"
