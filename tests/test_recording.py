"""Record/replay tests: the bridge from a live model run to a CI assertion.

The promise of a cassette is narrow and absolute: a run that happened once can
be re-executed for free and produce the same result. These tests hold that line.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from triage_agent.cli import DEMO_SCRIPT, _replay_matches
from triage_agent.config import Settings
from triage_agent.loop import run
from triage_agent.providers import (
    CassetteMismatch,
    MockProvider,
    ProviderError,
    RecordingProvider,
    _response_from_dict,
    _response_to_dict,
    canonical_request_hash,
    final_response,
    tool_call_response,
)
from triage_agent.types import Budget, Message, ToolSpec

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "resp",
    [
        tool_call_response("get_metric", {"name": "disk_pct", "samples": 10}, "c9"),
        final_response("done"),
    ],
)
def test_response_dict_round_trip(resp) -> None:
    back = _response_from_dict(_response_to_dict(resp))
    assert back.stop_reason == resp.stop_reason
    assert back.message.content == resp.message.content
    assert back.message.tool_calls == resp.message.tool_calls
    assert back.usage == resp.usage


def test_recorder_writes_a_replayable_cassette(tmp_path: Path) -> None:
    script = [
        tool_call_response("query_logs", {"limit": 3}, "c1"),
        final_response("done"),
    ]
    rec = RecordingProvider(
        MockProvider(script),
        tmp_path / "nested" / "cassette.json",
        metadata={"model": "test-model"},
    )
    assert rec.complete([Message.user("hi")], []).stop_reason == "tool_use"
    assert rec.complete([Message.user("hi")], []).message.content == "done"

    path = rec.save()
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["metadata"]["model"] == "test-model"
    assert data["metadata"]["turns"] == 2
    assert data["metadata"]["recorded_at"].endswith("+00:00")
    assert all(r.get("request_hash") for r in data["responses"])

    replay = MockProvider.from_cassette(path)
    first = replay.complete([Message.user("hi")], [])
    second = replay.complete([Message.user("hi")], [])
    assert first.message.tool_calls[0].name == "query_logs"
    assert first.message.tool_calls[0].arguments == {"limit": 3}
    assert second.message.content == "done"


class _Boom:
    """A provider that always fails, standing in for a 429 or a timeout."""

    def complete(self, *_args, **_kwargs):
        raise ProviderError("429 rate limited")


def test_failed_calls_are_not_recorded(tmp_path: Path) -> None:
    """A retried error must never end up in the cassette."""
    rec = RecordingProvider(_Boom(), tmp_path / "cassette.json")
    with pytest.raises(ProviderError):
        rec.complete([Message.user("hi")], [])
    assert rec.responses == []

    rec.save()
    data = json.loads((tmp_path / "cassette.json").read_text(encoding="utf-8"))
    assert data["responses"] == []


def _record_demo_run(tmp_path: Path):
    """Record one full loop run against an isolated copy of a real fixture."""
    shutil.copy(
        REPO / "victim" / "fixtures" / "bad_deploy.json", tmp_path / "state.json"
    )
    settings = Settings(
        provider="mock", model="mock-model", victim_root=str(tmp_path)
    )
    task = "Alert: checkout error rate spiked to 35% at 14:05."
    recorder = RecordingProvider(MockProvider(DEMO_SCRIPT), tmp_path / "cass.json")
    result = run(task, provider=recorder, settings=settings, budget=Budget(max_steps=8))
    return recorder.save(), task, settings, result


def test_recorded_run_replays_identically(tmp_path: Path) -> None:
    path, task, settings, result = _record_demo_run(tmp_path)
    assert result.status == "ok"
    assert result.budget is not None

    expected = (result.status, result.budget.steps_used)
    assert _replay_matches(str(path), task, settings, expected)


def test_replay_divergence_is_detected(tmp_path: Path) -> None:
    """A stale cassette must be caught, not silently trusted."""
    path, task, settings, result = _record_demo_run(tmp_path)
    assert result.budget is not None

    wrong = (result.status, result.budget.steps_used + 1)
    assert not _replay_matches(str(path), task, settings, wrong)


def test_request_hash_covers_tool_output_and_specs() -> None:
    """The hash must change for anything that legitimately changes a response."""
    base = [Message.user("investigate")]
    baseline = canonical_request_hash(base, [])

    assert canonical_request_hash(list(base), []) == baseline

    with_tool_output = [*base, Message.tool_result("c1", "disk 99%")]
    assert canonical_request_hash(with_tool_output, []) != baseline

    spec = ToolSpec(name="get_metric", description="d", input_schema={"type": "object"})
    assert canonical_request_hash(base, [spec]) != baseline


def test_stale_cassette_is_detected_when_tool_output_changes(tmp_path: Path) -> None:
    """The whole point: a changed tool response must not replay silently."""
    path, task, settings, result = _record_demo_run(tmp_path)
    assert result.status == "ok"

    # Same tool, same call, different output — exactly what a refactor does.
    state = tmp_path / "state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["logs"] = ["2026-10-17T13:55:02Z INFO  log format changed upstream"]
    state.write_text(json.dumps(data), encoding="utf-8")

    stale = run(
        task,
        provider=MockProvider.from_cassette(path),
        settings=settings,
        budget=Budget(max_steps=8),
    )
    assert stale.status == "cassette_stale"
    assert "stale" in (stale.reason or "").lower()
    assert "--record" in (stale.reason or "")


def test_cassette_without_hashes_still_replays(tmp_path: Path) -> None:
    """Backward compatibility: old hand-written cassettes keep working."""
    p = tmp_path / "legacy.json"
    p.write_text(
        json.dumps(
            {
                "responses": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": "c1", "name": "query_logs", "arguments": {}}
                            ],
                        },
                        "stop_reason": "tool_use",
                        "usage": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    resp = MockProvider.from_cassette(p).complete([Message.user("anything at all")], [])
    assert resp.stop_reason == "tool_use"


def test_cassette_mismatch_is_not_retryable() -> None:
    """If it were a ProviderError, call_with_retry would burn its attempts and
    then label a stale cassette as a transient provider failure."""
    assert not issubclass(CassetteMismatch, ProviderError)
