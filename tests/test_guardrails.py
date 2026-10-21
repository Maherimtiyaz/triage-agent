"""Guardrail tests. These are the ones that should scare you if they fail."""

from __future__ import annotations

from pathlib import Path

import pytest

from triage_agent.guardrails import PermissionDenied, Policy


def test_deny_by_default(policy: Policy) -> None:
    with pytest.raises(PermissionDenied):
        policy.check_tool("rm_rf_everything")


def test_sandbox_blocks_traversal(policy: Policy) -> None:
    """The classic bug: `../` escaping the root."""
    with pytest.raises(PermissionDenied):
        policy.resolve_path("../../../etc/passwd")
    with pytest.raises(PermissionDenied):
        policy.resolve_path("subdir/../../../../Windows/System32")


def test_sandbox_allows_inside(policy: Policy, victim_root: Path) -> None:
    resolved = policy.resolve_path("fixtures/default.json")
    assert resolved == (victim_root / "fixtures" / "default.json").resolve()


def test_run_cmd_off_by_default(policy: Policy) -> None:
    with pytest.raises(PermissionDenied, match="disabled"):
        policy.check_command(["ls"])


def test_run_cmd_allowlist(victim_root: Path) -> None:
    p = Policy(
        allowed_tools=frozenset({"run_cmd"}),
        allow_run_cmd=True,
        sandbox_root=victim_root,
    )
    p.check_command(["ls", "-la"])  # allowed
    with pytest.raises(PermissionDenied):
        p.check_command(["curl", "http://evil"])


def test_symlink_style_prefix_is_not_enough(tmp_path: Path) -> None:
    """Demonstrates WHY we use Path.resolve() and not startswith().

    A sibling dir named `victim-evil` shares a string prefix with `victim` but
    must never be reachable.
    """
    root = tmp_path / "victim"
    root.mkdir()
    evil = tmp_path / "victim-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text("nope")

    p = Policy(
        allowed_tools=frozenset({"read_file"}),
        allow_run_cmd=False,
        sandbox_root=root,
    )
    with pytest.raises(PermissionDenied):
        p.resolve_path("../victim-evil/secret.txt")
