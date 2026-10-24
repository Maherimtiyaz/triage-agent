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
    MockProvider,
    ProviderError,
    RecordingProvider,
    _response_from_dict,
    _response_to_dict,
    final_response,
    tool_call_response,
)
from triage_agent.types import Budget, Message

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

    replay = MockProvider.from_cassette(path)
    first = replay.complete([], [])
    second = replay.complete([], [])
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
