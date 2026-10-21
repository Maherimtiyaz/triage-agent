# Architecture

## The shape of a run

```
        task (alert text)
              │
              ▼
   ┌──────────────────────┐
   │  bootstrap           │  Settings -> Policy -> ToolRegistry -> Trace
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  budget check        │  steps / tokens / dollars  ──exhausted──▶ bail_out(reason)
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  context management  │  compact_if_needed()      ──evicted──▶ in-band notice
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  model call          │  call_with_retry(backoff) ──fails────▶ provider_error
   └──────────┬───────────┘
              ▼
        stop_reason?
        ├── tool_use ──▶ execute tools (timeout, policy, truncate)
        │                 └── append observations ──▶ loop again
        └── end_turn ──▶ validate structured output
                          ├── valid   ──▶ AgentResult(status="ok", report=...)
                          └── invalid ──▶ retry with error feedback (≤2) ──▶ invalid_output
```

Every terminal path returns an `AgentResult` with a `status`. Nothing the model
does can raise out of `run()` except a bug in the harness itself.

## Why the seams are where they are

| Seam | Type | Why it matters |
|---|---|---|
| `Provider` | protocol | Swap vendors without touching the loop. Enables the mock provider, which is what makes evals deterministic. |
| `ToolSpec` | dataclass | The schema the model sees. Changing it changes model behaviour — treat it as code. |
| `Policy` | dataclass | The only thing that authorises an action. It never reads model output, so the model cannot argue its way past it. |
| `IncidentReport` | pydantic model | The contract with downstream systems. Validated, not parsed. |
| `Trace` | append-only events | The only reason you can debug a run after the fact. |

## Data flow for one tool call

```
model returns ToolCall(name, arguments)
        │
        ▼
  registry.dispatch_with_timeout
        │
        ├─ unknown tool?          -> ERROR string back to model  (ok=False)
        ├─ policy.check_tool?     -> DENIED string back to model (ok=False)
        ├─ handler missing?       -> ERROR                       (ok=False)
        ├─ TypeError (bad args)?  -> ERROR                       (ok=False)
        ├─ PermissionDenied?      -> DENIED                      (ok=False)
        ├─ other exception?       -> ERROR                       (ok=False)
        ├─ exceeded timeout?      -> ERROR                       (ok=False)
        └─ success               -> stringify -> truncate(head+tail) -> (ok=True)
```

**Nothing here raises.** Every failure becomes an observation the model can read
and react to. That single property is what makes the agent robust to its own
mistakes.

## Invariants worth preserving

1. `run()` returns; it does not raise for model/tool/provider problems.
2. A tool call cannot exceed its timeout.
3. Total spend is bounded by `Budget.max_cost_usd` regardless of step count.
4. A retried provider call does not consume a step.
5. Model output that is acted upon is schema-validated.
6. The model is told when context was evicted.
7. Deny-by-default: absence from `allowed_tools` means the tool cannot run.

Each invariant has a test in `tests/`. If you change one, change its test too —
and explain why in the commit body.

## Extending

- **New tool** → `tools.py` (`ToolSpec` + handler + register) → add to
  `TRIAGE_ALLOWED_TOOLS` → test.
- **New provider** → `providers.py` (implement `complete`) → wire in
  `build_provider`.
- **New incident** → `victim/faults.py` + fixture + cassette + `DATASET` row.
- **New terminal status** → `loop.py` (add to `Status`) → handle in `cli.py`.

## Deliberate non-goals

- **No remediation.** The agent suggests; it never acts. Keeps the blast radius
  at zero and keeps prompt injection from being a critical bug (see `SECURITY.md`).
- **No framework.** No LangChain/LlamaIndex. The point is to see the whole
  harness, ~1k lines you can read in one sitting.
- **No streaming.** Adds surface area without teaching a new production lesson.
- **No database.** State is files on disk so a run is inspectable and replayable.
