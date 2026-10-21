"""The tool layer: schemas the model sees, and the guarded dispatch that runs them.

The central production insight: THE TOOL SCHEMA IS THE PROMPT. A vague
description produces vague calls. A tool that dumps 50 MB of logs kills the
context window on step 2. Design tools as if the model is a smart, extremely
literal junior engineer who cannot see your codebase.

Three rules this module enforces:
  1. Every tool call is bounded by a timeout (a tool that hangs hangs the run).
  2. Tool failures are RETURNED, not raised. The model reads the error and can
     recover. An exception would abort a 20-step run over a typo'd path.
  3. Results are truncated to a model-sized budget. Context is scarce.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from triage_agent.guardrails import PermissionDenied, Policy
from triage_agent.types import ToolCall, ToolSpec

# --------------------------------------------------------------------------- #
# Schemas — what the model is allowed to ask for
# --------------------------------------------------------------------------- #

QUERY_LOGS = ToolSpec(
    name="query_logs",
    description=(
        "Search the service's application logs. Returns matching log lines, "
        "newest first. Use `level` to filter severity and `contains` for a "
        "substring match (e.g. 'error', 'timeout', 'certificate'). Prefer a "
        "narrow `contains` over pulling everything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": ["DEBUG", "INFO", "WARN", "ERROR"]},
            "contains": {"type": "string", "description": "substring to match"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        "required": [],
    },
    max_result_chars=6_000,
)

LIST_DEPLOYS = ToolSpec(
    name="list_deploys",
    description=(
        "List recent deployments, newest first, with version, timestamp and "
        "the git commit subject. Use this to correlate an incident onset with "
        "a recent change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
        },
        "required": [],
    },
)

GET_METRIC = ToolSpec(
    name="get_metric",
    description=(
        "Fetch a time series for one metric. Available metrics: "
        "request_latency_p99_ms, error_rate_pct, cpu_pct, mem_pct, disk_pct, "
        "db_connections. Returns the last N samples."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": [
                    "request_latency_p99_ms",
                    "error_rate_pct",
                    "cpu_pct",
                    "mem_pct",
                    "disk_pct",
                    "db_connections",
                ],
            },
            "samples": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["name"],
    },
)

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a file from inside the service's configuration/source tree. "
        "Paths are relative to the service root. Use for config files, "
        "Dockerfiles, and manifests."
    ),
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)

RUN_CMD = ToolSpec(
    name="run_cmd",
    description=(
        "Run a read-only diagnostic command on the host. Pass argv as a list, "
        "e.g. ['df', '-h']. Only a small allowlist of read-only commands is "
        "permitted. Disabled unless explicitly enabled."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "command and arguments, e.g. ['df','-h']",
            }
        },
        "required": ["argv"],
    },
    dangerous=True,
)

ALL_SPECS: dict[str, ToolSpec] = {
    s.name: s for s in (QUERY_LOGS, LIST_DEPLOYS, GET_METRIC, READ_FILE, RUN_CMD)
}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


@dataclass
class ToolOutcome:
    """Everything the tracer and the model need to know about one call."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    content: str
    ok: bool
    error: str | None = None
    truncated: bool = False


@dataclass
class ToolRegistry:
    """Binds specs to implementations and executes them safely."""

    policy: Policy
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    _pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=4))

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self.handlers[name] = fn

    def specs(self, include_dangerous: bool = True) -> list[ToolSpec]:
        """Only advertise tools the policy actually allows. Advertising a tool
        the model can't run is how you get wasted steps and confused behavior.
        """
        out = []
        for name, spec in ALL_SPECS.items():
            if name not in self.policy.allowed_tools:
                continue
            if spec.dangerous and not self.policy.allow_run_cmd:
                continue
            out.append(spec)
        return out

    def dispatch(self, call: ToolCall) -> ToolOutcome:
        spec = ALL_SPECS.get(call.name)
        if spec is None:
            # Unknown tool: return an actionable error, don't crash.
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"ERROR: no tool named '{call.name}'. Available: "
                f"{[s.name for s in self.specs()]}",
                ok=False, error="unknown_tool",
            )

        try:
            self.policy.check_tool(call.name)
        except PermissionDenied as exc:
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"DENIED: {exc}", ok=False, error="permission_denied",
            )

        handler = self.handlers.get(call.name)
        if handler is None:
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"ERROR: tool '{call.name}' has no implementation bound",
                ok=False, error="unbound_tool",
            )

        # Malformed args are the most common failure. Catch and feed back.
        try:
            raw = handler(**call.arguments)
        except TypeError as exc:
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"ERROR: bad arguments for '{call.name}': {exc}",
                ok=False, error="bad_arguments",
            )
        except PermissionDenied as exc:
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"DENIED: {exc}", ok=False, error="permission_denied",
            )
        except Exception as exc:  # tool bug must not abort the run
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"ERROR: tool '{call.name}' failed: {type(exc).__name__}: {exc}",
                ok=False, error="tool_exception",
            )

        text, truncated = truncate_result(_stringify(raw), spec.max_result_chars)
        return ToolOutcome(call.id, call.name, call.arguments, text, ok=True, truncated=truncated)

    def dispatch_with_timeout(self, call: ToolCall, timeout_s: float) -> ToolOutcome:
        """Wall-clock cap. A tool that hangs must not hang the run."""
        future = self._pool.submit(self.dispatch, call)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            return ToolOutcome(
                call.id, call.name, call.arguments,
                content=f"ERROR: tool '{call.name}' timed out after {timeout_s}s",
                ok=False, error="timeout",
            )


def _stringify(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, indent=2, default=str)


def truncate_result(text: str, max_chars: int) -> tuple[str, bool]:
    """Head + tail truncation.

    Naive `text[:n]` throws away the END of a stack trace or the tail of a log,
    which is usually where the answer is. Keeping head AND tail is the cheap
    trick that preserves both the request and the error.
    """
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        f"{text[:head]}\n\n... [{omitted} chars omitted — refine your query to "
        f"see more] ...\n\n{text[-tail:]}",
        True,
    )


# --------------------------------------------------------------------------- #
# Implementations against the victim application
# --------------------------------------------------------------------------- #


def build_registry(policy: Policy) -> ToolRegistry:
    """Wire the real implementations. Each closes over `policy` so the sandbox
    boundary is enforced at the point of use, not at the call site."""
    from triage_agent.victim_data import load_state

    reg = ToolRegistry(policy=policy)

    def query_logs(
        level: str | None = None, contains: str | None = None, limit: int = 50
    ) -> str:
        lines = load_state(policy.sandbox_root).logs
        if level:
            lines = [ln for ln in lines if f" {level} " in ln]
        if contains:
            needle = contains.lower()
            lines = [ln for ln in lines if needle in ln.lower()]
        if not lines:
            return "no matching log lines"
        return "\n".join(lines[-limit:][::-1])

    def list_deploys(limit: int = 10) -> str:
        deploys = load_state(policy.sandbox_root).deploys
        return "\n".join(
            f"{d['ts']}  {d['version']}  {d['commit']}" for d in deploys[-limit:][::-1]
        ) or "no deploys recorded"

    def get_metric(name: str, samples: int = 20) -> str:
        series = load_state(policy.sandbox_root).metrics.get(name)
        if not series:
            return f"unknown metric '{name}'"
        pts = series[-samples:]
        return f"{name} (last {len(pts)} samples):\n" + "\n".join(
            f"  {t}  {v}" for t, v in pts
        )

    def read_file(path: str) -> str:
        target = policy.resolve_path(path)  # raises PermissionDenied on escape
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def run_cmd(argv: list[str]) -> str:
        policy.check_command(argv)
        import subprocess

        proc = subprocess.run(  # noqa: S603 - argv form, no shell, allowlisted
            argv, capture_output=True, text=True, timeout=5, shell=False
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or f"(no output, exit {proc.returncode})"

    reg.register("query_logs", query_logs)
    reg.register("list_deploys", list_deploys)
    reg.register("get_metric", get_metric)
    reg.register("read_file", read_file)
    reg.register("run_cmd", run_cmd)
    return reg
