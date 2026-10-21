# Changelog

All notable changes are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-19

First working harness. One task: given an alert, investigate a service and
produce a schema-validated incident report.

### Added

- **Agent loop** (`loop.py`) with an explicit terminal state, three-axis budget
  (steps / tokens / dollars), and graceful `budget_exhausted` bail-out.
- **Tool layer** (`tools.py`) — five tools with JSON Schemas, per-tool timeouts,
  head+tail result truncation, and errors returned to the model instead of raised.
- **Guardrails** (`guardrails.py`) — deny-by-default allowlist, path sandbox that
  resolves `..` and symlinks, and `run_cmd` behind a feature flag + argv allowlist
  + no-shell execution.
- **Structured output** (`output.py`) — `IncidentReport` validated with pydantic,
  with bounded retry that feeds the validation error back to the model.
- **Observability** (`tracing.py`) — per-step token, latency and cost accounting,
  a human-readable timeline, and JSON dump.
- **Failure handling** (`providers.py`) — exponential backoff with jitter; a retry
  does not consume a step.
- **Context compaction** (`context.py`) — head/tail preservation with an in-band
  notice so the model knows eviction happened.
- **Victim service** (`victim/`) — a stdlib HTTP service you own, with plantable
  faults (`bad_deploy`, `slow_query`, `disk_full`, `cert_expired`).
- **Eval harness** (`evals/`) — golden dataset, keyword scorer, recorded cassettes
  so evals are deterministic and free, and a CI gate.
- **Providers** — a scripted `MockProvider` with record/replay and an
  `AnthropicProvider` adapter.
- 35 tests covering guardrails, tools, output validation, context, and loop
  behaviour (budgets, retries, provider errors, invalid output).

[0.1.0]: https://github.com/Maherimtiyaz/triage-agent/releases/tag/v0.1.0
