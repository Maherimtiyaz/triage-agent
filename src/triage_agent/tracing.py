"""Observability: a trace of every step, with tokens, latency and cost.

The production test for observability is: "an agent run failed yesterday at
3am — can you explain what it did, and what it cost?" If the honest answer is
no, you have a demo, not a system.

A trace is just an append-only list of typed events. Ship it to a log, a JSON
file, or an OTel backend — the shape is what matters.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

EventKind = Literal["run_start", "llm_call", "tool_call", "compact", "output_retry", "run_end"]


@dataclass
class TraceEvent:
    kind: EventKind
    step: int
    elapsed_ms: float
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    """Collects events; can dump to JSON for offline analysis."""

    run_id: str
    events: list[TraceEvent] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def _now_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def record(self, kind: EventKind, step: int, **data: Any) -> None:
        self.events.append(TraceEvent(kind=kind, step=step, elapsed_ms=self._now_ms(), data=data))

    # convenience wrappers keep call sites readable
    def llm(self, step: int, *, input_tokens: int, output_tokens: int, stop_reason: str,
            latency_ms: float, cost_usd: float, tool_names: list[str]) -> None:
        self.record(
            "llm_call", step,
            input_tokens=input_tokens, output_tokens=output_tokens,
            stop_reason=stop_reason, latency_ms=round(latency_ms, 1),
            cost_usd=round(cost_usd, 6), tool_names=tool_names,
        )

    def tool(self, step: int, *, name: str, ok: bool, error: str | None,
             truncated: bool, latency_ms: float, args: dict) -> None:
        self.record(
            "tool_call", step, name=name, ok=ok, error=error,
            truncated=truncated, latency_ms=round(latency_ms, 1), args=args,
        )

    def summary(self) -> dict[str, Any]:
        llm = [e for e in self.events if e.kind == "llm_call"]
        tools = [e for e in self.events if e.kind == "tool_call"]
        return {
            "run_id": self.run_id,
            "steps": len(llm),
            "tool_calls": len(tools),
            "tool_errors": sum(1 for t in tools if not t.data.get("ok")),
            "tool_time_ms": round(sum(t.data.get("latency_ms", 0) for t in tools), 1),
            "input_tokens": sum(e.data.get("input_tokens", 0) for e in llm),
            "output_tokens": sum(e.data.get("output_tokens", 0) for e in llm),
            "cost_usd": round(sum(e.data.get("cost_usd", 0) for e in llm), 6),
            "wall_ms": self._now_ms(),
        }

    def dump(self, path: str | Path) -> None:
        payload = {
            "summary": self.summary(),
            "events": [asdict(e) for e in self.events],
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def render(self) -> str:
        """A human-readable timeline — this is what you read at 3am."""
        lines = []
        for e in self.events:
            if e.kind == "llm_call":
                d = e.data
                lines.append(
                    f"[{e.elapsed_ms:8.1f}ms] step {e.step} LLM  "
                    f"in={d['input_tokens']:<6} out={d['output_tokens']:<5} "
                    f"${d['cost_usd']:.4f}  -> {d['stop_reason']}"
                    + (f" tools={d['tool_names']}" if d["tool_names"] else "")
                )
            elif e.kind == "tool_call":
                d = e.data
                mark = "ok " if d["ok"] else "ERR"
                lines.append(
                    f"[{e.elapsed_ms:8.1f}ms] step {e.step} TOOL {mark} "
                    f"{d['name']}({_short(d['args'])}) {d['latency_ms']}ms"
                    + (f" error={d['error']}" if d["error"] else "")
                    + (" [truncated]" if d["truncated"] else "")
                )
            else:
                lines.append(f"[{e.elapsed_ms:8.1f}ms] {e.kind}: {_short(e.data)}")
        return "\n".join(lines)


def _short(d: Any, n: int = 70) -> str:
    s = json.dumps(d, default=str)
    return s if len(s) <= n else s[:n] + "..."
