"""Loads the victim service's "observability data" from disk.

The agent's tools read from here. Keeping the data on disk (not in memory)
means a planted fault survives between runs and the whole thing is inspectable
— which matters enormously when you are debugging the agent itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class VictimState:
    logs: list[str]
    deploys: list[dict]
    metrics: dict[str, list[list]]


def load_state(root: Path) -> VictimState:
    """Read `state.json` (or `fixtures/<scenario>.json`) under the sandbox root.

    Falls back to empty state so a missing fixture yields a clear "no logs"
    rather than a crash — the agent can then report 'no data' honestly.
    """
    for candidate in (root / "state.json", root / "fixtures" / "default.json"):
        if candidate.is_file():
            return _parse(candidate)
    return VictimState(logs=[], deploys=[], metrics={})


# The state file changes between evals; cache by (path, mtime) so we re-read.
@lru_cache(maxsize=32)
def _parse_cached(path_str: str, mtime: float) -> VictimState:
    data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    return VictimState(
        logs=list(data.get("logs", [])),
        deploys=list(data.get("deploys", [])),
        metrics={k: list(v) for k, v in data.get("metrics", {}).items()},
    )


def _parse(path: Path) -> VictimState:
    return _parse_cached(str(path), path.stat().st_mtime)
