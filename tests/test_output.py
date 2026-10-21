"""Output validation tests: the model is untrusted input."""

from __future__ import annotations

import json

from triage_agent.output import parse_report

VALID = {
    "severity": "SEV2",
    "summary": "Checkout error rate spiked to 35% after the 13:58 deploy.",
    "root_cause": "Deploy v2.4.1 regressed the payments client.",
    "confidence": 0.8,
    "evidence": [{"source": "get_metric", "detail": "error_rate_pct 0.4 -> 35.0"}],
    "suggested_fix": "Roll back to v2.4.0.",
    "suspected_deploy": "v2.4.1",
}


def test_parses_bare_json() -> None:
    report, err = parse_report(json.dumps(VALID))
    assert err is None and report is not None
    assert report.severity == "SEV2"


def test_parses_fenced_json_with_prose() -> None:
    text = "Here is the report:\n```json\n" + json.dumps(VALID) + "\n```\nHope that helps!"
    report, err = parse_report(text)
    assert err is None and report is not None


def test_rejects_missing_evidence() -> None:
    bad = {**VALID, "evidence": []}
    report, err = parse_report(json.dumps(bad))
    assert report is None
    assert err is not None and "evidence" in err


def test_rejects_bad_confidence() -> None:
    bad = {**VALID, "confidence": 1.7}
    report, err = parse_report(json.dumps(bad))
    assert report is None
    assert err is not None and "confidence" in err


def test_rejects_non_json() -> None:
    report, err = parse_report("I could not determine the cause.")
    assert report is None
    assert err is not None and "JSON" in err
