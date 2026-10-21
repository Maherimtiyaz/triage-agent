# triage-agent

**A production-style harness for one task:** given an alert, investigate a
service and produce a schema-validated incident report.

Most "AI agent" repos stop at the loop and call it done. This one is about
everything *around* the loop — the part that decides whether a system survives
contact with production.

[![CI](https://github.com/OWNER/triage-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/triage-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-35%20passing-brightgreen)
![Evals](https://img.shields.io/badge/evals-3%2F3-success)

> Replace `OWNER` with your GitHub username after forking (see
> [Publishing](#publishing) below).

```
$ python evals/runner.py
case         status             pass  steps  cost     missing
------------------------------------------------------------------------
bad_deploy   ok                 True  4      $0.0000  -
slow_query   ok                 True  3      $0.0000  -
disk_full    ok                 True  3      $0.0000  -
------------------------------------------------------------------------
PASSED 3/3  (100%)
```

It runs **offline, with no API key, for free** — the default provider replays
recorded model responses. Flip two environment variables to point it at a real
model.

---

## Table of contents

- [The one idea](#the-one-idea)
- [Quickstart](#quickstart)
- [Using a real model](#using-a-real-model)
- [What it does](#what-it-does)
- [The seven subsystems](#the-seven-subsystems)
- [Configuration reference](#configuration-reference)
- [Architecture](#architecture)
- [Extending it](#extending-it)
- [Testing and evals](#testing-and-evals)
- [Project layout](#project-layout)
- [Publishing](#publishing)
- [Further reading](#further-reading)

---

## The one idea

> The model is not your product. The model is an **unreliable function inside
> your deterministic system.** The harness makes the unreliable thing reliable.

A toy calls the model and prints the text. A harness bounds it, sandboxes it,
validates its output, traces it, and scores it. This repo is the harness.

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/OWNER/triage-agent.git
cd triage-agent

# Windows (PowerShell)
py -m venv .venv
.\.venv\Scripts\Activate.ps1

# macOS / Linux
# python3 -m venv .venv && source .venv/bin/activate

pip install -e ".[dev]"

pytest -q                 # 35 tests
python evals/runner.py    # 3/3
ruff check .              # clean
```

Now run one investigation end-to-end and watch the trace:

```bash
$env:PYTHONPATH="src"     # PowerShell; or use `pip install -e .` and skip this
python -m triage_agent "Alert: checkout error rate spiked to 35% at 14:05."
```

You will get a timeline of every step, the token/cost summary, and the final
report:

```
[     0.1ms] step 1 LLM  in=100    out=20    $0.0000  -> tool_use tools=['list_deploys']
[     3.9ms] step 1 TOOL ok  list_deploys({"limit": 5}) 3.8ms
[     4.0ms] step 2 LLM  in=100    out=20    $0.0000  -> tool_use tools=['query_logs']
[     4.4ms] step 2 TOOL ok  query_logs({"level": "ERROR", "limit": 20}) 0.4ms
[     4.8ms] step 3 TOOL ok  get_metric({"name": "error_rate_pct"}) 0.3ms
[     6.5ms] run_end: {"status": "ok", "severity": "SEV2"}
```

## Using a real model

```bash
cp .env.example .env
pip install -e ".[anthropic]"
```

Then set, in your shell or `.env`:

```bash
TRIAGE_PROVIDER=anthropic
TRIAGE_MODEL=claude-sonnet-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

Run it against a real model and watch the trace. Set `TRIAGE_MAX_STEPS=40` the
first time — you will see the model repeat queries, which is usually a
tool-description bug. That is the lesson.

> **Costs real money.** Budgets are enforced (`TRIAGE_MAX_COST_USD`), but start
> with `TRIAGE_MAX_COST_USD=0.25`.

---

## What it does

You run a small HTTP service (`victim/`) and **plant a fault** in it — a bad
deploy, a slow query, a full disk, an expired certificate. You hand the agent an
alert. The agent investigates using only its tools, and returns:

```json
{
  "severity": "SEV2",
  "summary": "Checkout began failing after the 13:58 deploy, affecting ~35% of traffic.",
  "root_cause": "Deploy v2.4.1 regressed the payments client, causing a 35% error rate.",
  "confidence": 0.82,
  "evidence": [
    { "source": "list_deploys", "detail": "v2.4.1 deployed at 13:58, minutes before onset" },
    { "source": "query_logs",   "detail": "ERROR 'upstream 500 from payments' after 14:00" },
    { "source": "get_metric",   "detail": "error_rate_pct rose from 0.4 to 35.0" }
  ],
  "suggested_fix": "Roll back to v2.4.0 and investigate the payments client change.",
  "suspected_deploy": "v2.4.1"
}
```

The tools available to the model:

| Tool | Purpose | Guardrail |
|---|---|---|
| `query_logs` | search logs by level / substring | result capped at 6k chars |
| `list_deploys` | recent deploys with commit subjects | read-only |
| `get_metric` | time series for a known metric | metric name is an enum |
| `read_file` | read config/source from the service tree | sandboxed to one root |
| `run_cmd` | read-only host diagnostics | **off by default**, argv allowlist, no shell |

## The seven subsystems

| # | Subsystem | File | The failure it prevents |
|---|-----------|------|--------------------------|
| 1 | Agent loop | `loop.py` | Infinite loops; no terminal state |
| 2 | Tool layer | `tools.py` | Malformed calls, huge payloads, tool crashes |
| 3 | Context management | `context.py` | `context_length_exceeded` mid-run |
| 4 | Guardrails | `guardrails.py` | Path traversal, un-allowlisted commands |
| 5 | Observability | `tracing.py` | "It failed and I have no idea why" |
| 6 | Evals | `evals/` | Prompt edits silently breaking cases |
| 7 | Failure handling | `providers.py`, `loop.py` | One rate-limit killing a 20-step run |

Each milestone is tagged inline in `loop.py` as `# M0` … `# M6`.

### M0 · The loop

`while True:` → budget check → model → tools → observe → finalize.

**The lesson:** an agent is a state machine with a *terminal state*. `max_steps`
isn't paranoia — a model that keeps calling tools forever is a bill.

### M1 · Tools + guardrails

Five tools, each with a JSON Schema, a timeout, and a result cap. Tools
**return** errors; they never raise, so the model can recover from a typo'd path
at step 12 instead of losing the run.

`run_cmd` sits behind three gates: a feature flag, an argv allowlist, and
`shell=False`.

**The lesson:** the blast radius is the design. Deny-by-default is not a config
option — it's the architecture.

### M2 · Structured output

The answer is a pydantic model, not prose. On validation failure the error is fed
back and the model retries — bounded at 2 attempts.

**The lesson:** never regex-parse model output and hope. The model is untrusted
input. Either it returns a conforming report, or it reports that it couldn't.

### M3 · Observability

Every step records tokens, latency, cost, tool arguments, and whether the result
was truncated.

**The lesson:** if you can't answer *"why did this take 14 steps and $0.31?"*, you
have a demo.

### M4 · Failure handling

Exponential backoff with jitter. A retried call does **not** consume a step.
Budget exhaustion returns `status="budget_exhausted"` with a reason — a normal
result, not a crash.

**The lesson:** transient failures are normal; a 429 at step 19 must not destroy
19 steps of work.

### M5 · Evals

A golden dataset, a scorer, recorded cassettes, and a CI gate that fails the build
if accuracy drops.

**The lesson:** prompt changes are code changes. Without evals you are guessing.

### M6 · Context compaction

Deterministic head/tail preservation, with an in-band notice telling the model
that eviction happened.

**The lesson:** do **not** silently drop messages — the model then re-runs queries
it already ran. If you evict, say so.

---

## Configuration reference

All configuration is environment-driven. Start from `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `TRIAGE_PROVIDER` | `mock` | `mock` (offline, free) or `anthropic` |
| `TRIAGE_MODEL` | `mock-model` | Model id; drives pricing lookup |
| `ANTHROPIC_API_KEY` | — | Required only for `anthropic` |
| `TRIAGE_VICTIM_ROOT` | `victim` | Sandbox root the agent may read |
| `TRIAGE_ALLOWED_TOOLS` | `query_logs,list_deploys,get_metric,read_file` | Comma-separated allowlist |
| `TRIAGE_ALLOW_RUN_CMD` | `0` | Enables the `run_cmd` tool |
| `TRIAGE_MAX_STEPS` | `25` | Hard step ceiling |
| `TRIAGE_MAX_COST_USD` | `1.0` | Hard spend ceiling |
| `TRIAGE_TOOL_TIMEOUT_S` | `10` | Per-tool wall clock |

Model pricing lives in `config.py::MODEL_PRICING`. **Update it when you change
models** — stale pricing silently corrupts cost attribution.

## Architecture

A run is a bounded loop with four possible terminal states:

```
task → [budget → compact → model → tools]* → report | budget_exhausted |
                                            provider_error | invalid_output
```

`run()` never raises for model, tool, or provider problems — it always returns an
`AgentResult` with a `status` and, on success, a validated `IncidentReport`.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full diagram, the
data flow of a single tool call, and the invariants each test protects.

## Extending it

### Add a tool

1. Define a `ToolSpec` in `tools.py` — the description *is* part of the prompt.
2. Implement the handler and register it in `build_registry`.
3. Add the name to the default allowlist in `config.py`.
4. Add a success **and** failure test in `tests/test_tools.py`.

### Add an incident scenario

This is the production loop in miniature — do this one first:

1. Plant the fault in `victim/faults.py`.
2. Add `victim/fixtures/<name>.json` (logs, deploys, metrics that corroborate it).
3. Record `evals/cassettes/<name>.json`.
4. Add a `Case(...)` row to `DATASET` in `evals/runner.py`.

### Swap the model vendor

Implement the `Provider` protocol in `providers.py` (one method: `complete`).
Keep vendor SDK imports lazy so the base install stays dependency-light.

## Testing and evals

Three layers, all run in CI:

```bash
ruff check .              # style + correctness
pytest tests -q           # unit + behavioral  (35 tests)
pytest evals -q           # the eval gate
```

**Why evals are separate from tests.** Unit tests assert that *code* works.
Evals assert that the *agent* is still good. They use a golden dataset of
incidents with known root causes and a keyword scorer over `root_cause`. Because
the provider is injectable, they replay recorded cassettes — deterministic, free,
and fast enough for every commit.

When you change the system prompt or a tool description, the eval tells you
exactly which scenarios regressed. That is the feedback loop that makes prompt
iteration safe.

**The gate:** `PASS_THRESHOLD = 1.0` in `evals/test_evals.py`. Raise the bar by
adding cases, not by lowering the threshold.

## Project layout

```
src/triage_agent/
  types.py        wire types + Budget            (the vendor seam)
  config.py       env settings, model pricing
  providers.py    Mock + Anthropic + retry       (M4)
  tools.py        schemas + guarded dispatch     (M1, M2)
  guardrails.py   policy, sandbox, cost          (M1)
  output.py       IncidentReport validation      (M2)
  tracing.py      trace events + summary         (M3)
  context.py      compaction                     (M6)
  loop.py         THE LOOP                       (all)
  victim_data.py  loads the victim's observability data
  cli.py          run one investigation
victim/           the toy service you own + plantable faults
evals/            dataset, scorer, cassettes, CI gate   (M5)
tests/            unit + behavioral tests
docs/             architecture deep-dive
```

## Publishing

After forking, replace `OWNER` in these files with your GitHub username:

- `README.md` (badge URLs)
- `pyproject.toml` (`[project.urls]`)
- `CHANGELOG.md` (release link)

Then:

```bash
git init -b main
git add .
git commit -m "chore: initial commit"
git remote add origin https://github.com/OWNER/triage-agent.git
git push -u origin main
```

CI (`.github/workflows/ci.yml`) runs automatically on push: lint → tests → eval
gate.

## Further reading

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the full run diagram and invariants
- [`SECURITY.md`](SECURITY.md) — sandboxing, prompt injection, secrets
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — the change workflow and commit style
- [`CHANGELOG.md`](CHANGELOG.md) — what shipped in 0.1.0

## License

MIT — see [LICENSE](LICENSE).
