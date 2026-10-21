"""Environment-driven settings. No secrets in code, ever."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# USD per 1M tokens. Update these — stale pricing silently corrupts your cost
# dashboards, and cost is a real production SLO for agents.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
    "mock-model": {"input": 0.0, "output": 0.0, "cache_read": 0.0},
}


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.environ.get("TRIAGE_PROVIDER", "mock"))
    model: str = field(default_factory=lambda: os.environ.get("TRIAGE_MODEL", "mock-model"))
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )

    # The "prod" system under investigation. Root it at a directory so the
    # read_file tool has a hard sandbox boundary (see tools.py).
    victim_root: str = field(default_factory=lambda: os.environ.get("TRIAGE_VICTIM_ROOT", "victim"))

    # Which tools may run at all. Dangerous tools must be opted INTO, not
    # out of — deny-by-default is the whole point.
    allowed_tools: frozenset[str] = field(
        default_factory=lambda: frozenset(
            filter(
                None,
                os.environ.get(
                    "TRIAGE_ALLOWED_TOOLS",
                    "query_logs,list_deploys,get_metric,read_file",
                ).split(","),
            )
        )
    )
    # run_cmd is the blast radius. Separate switch, default OFF.
    allow_run_cmd: bool = field(
        default_factory=lambda: os.environ.get("TRIAGE_ALLOW_RUN_CMD", "").lower()
        in ("1", "true", "yes")
    )

    max_steps: int = field(default_factory=lambda: _i("TRIAGE_MAX_STEPS", 25))
    max_cost_usd: float = field(default_factory=lambda: _f("TRIAGE_MAX_COST_USD", 1.0))
    tool_timeout_s: float = field(default_factory=lambda: _f("TRIAGE_TOOL_TIMEOUT_S", 10.0))


def load_settings() -> Settings:
    return Settings()
