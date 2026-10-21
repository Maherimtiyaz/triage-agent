"""Guardrails: budgets, permissions, sandboxing, and output validation.

Two ideas carry this whole module:

1. DENY BY DEFAULT. A tool runs only if it is on the allowlist. Dangerous
   tools need a second, deliberate opt-in. The model cannot talk its way past
   a policy check because the check does not read model output.

2. VALIDATE, DON'T TRUST. Anything the model returns that your system then
   *acts on* gets checked against a schema. On failure you retry with the
   validation error fed back — the model is usually capable of fixing its own
   malformed output when told exactly what was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from triage_agent.types import Budget


class PermissionDenied(Exception):
    """Raised inside a tool when policy forbids the action."""


@dataclass
class Policy:
    """The blast-radius controller for tools."""

    allowed_tools: frozenset[str]
    allow_run_cmd: bool
    sandbox_root: Path
    cmd_allowlist: frozenset[str] = frozenset(
        {"ls", "cat", "tail", "head", "grep", "df", "du", "ps", "env", "date", "uptime"}
    )

    def check_tool(self, name: str) -> None:
        if name not in self.allowed_tools:
            raise PermissionDenied(
                f"tool '{name}' is not on the allowlist. "
                f"Allowed: {sorted(self.allowed_tools)}"
            )

    def check_command(self, argv: list[str]) -> None:
        """run_cmd is the one tool that can destroy the host. Three gates:
        the feature flag, the allowlist, and a no-shell-exec rule (the caller
        must pass argv, never a string, so pipes/redirects can't sneak in).
        """
        if not self.allow_run_cmd:
            raise PermissionDenied(
                "run_cmd is disabled. Set TRIAGE_ALLOW_RUN_CMD=1 to enable it."
            )
        if not argv:
            raise PermissionDenied("empty command")
        if argv[0] not in self.cmd_allowlist:
            raise PermissionDenied(
                f"command '{argv[0]}' is not allowlisted. "
                f"Allowed: {sorted(self.cmd_allowlist)}"
            )

    def resolve_path(self, relative: str) -> Path:
        """Path sandbox. `Path.resolve()` collapses `..`, then we assert the
        result is still inside the root. This is the classic path-traversal bug
        and it is trivial to get wrong with naive string prefix checks.
        """
        root = self.sandbox_root.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise PermissionDenied(f"path '{relative}' escapes sandbox root {root}")
        return candidate


def account_usage(budget: Budget, input_tokens: int, output_tokens: int, price: dict) -> None:
    """Charge tokens and dollars to the live budget."""
    budget.tokens_used += input_tokens + output_tokens
    budget.cost_usd += (
        input_tokens * price.get("input", 0.0)
        + output_tokens * price.get("output", 0.0)
    ) / 1_000_000
