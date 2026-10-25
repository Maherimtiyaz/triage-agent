# Changelog

All notable changes are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added

- **`OpenAICompatibleProvider`** — works with any `POST /v1/chat/completions`
  endpoint: Ollama, OmniRoute, LM Studio, llama.cpp, vLLM, OpenRouter. Written
  against stdlib `urllib`, so the base install still has no new dependency.
- **Free-model support** — the agent now runs end-to-end with no API key via
  Ollama (`gemma4:31b-cloud`, free on Ollama's free plan, ~1.5s/step).
- **`--scenario` flag** — `python -m triage_agent --scenario disk_full "..."` runs
  against exactly one fixture, so the alert matches the data.
- 9 provider tests covering wire-format conversion, malformed tool arguments,
  stop-reason mapping, and connection failures surfacing as `ProviderError`.
- **`--record PATH`** — records a live model run into a replayable cassette and
  immediately verifies that the cassette reproduces the same terminal status and
  step count before you commit it.
- **`--replay PATH`** — re-runs a task from a cassette with no model, key, or
  network. Same report, ~3.6 s.
- **`RecordingProvider`** — wraps any provider, persists successful responses in
  the exact shape `MockProvider.from_cassette` already consumes, and stamps
  provenance (`provider`, `model`, `scenario`, `task`, `recorded_at`, `turns`).
  Failed calls are never recorded, so a retried `429` cannot poison a replay.
- 10 recording tests covering dict round-trip, cassette shape, failure
  exclusion, end-to-end replay fidelity, divergence detection, and staleness.
- **Verifiable cassettes** — every recorded response now carries a SHA-256 of the
  exact request that produced it: the conversation including tool results, plus
  the tool specs. Replay re-hashes the request and raises `CassetteMismatch` when
  it moved, so a changed tool output, a changed schema, or an edited system
  prompt can no longer replay silently against a stale transcript.
- **`cassette_stale` status** — the loop turns a mismatch into a first-class
  terminal state carrying the turn number and a re-record instruction, rather
  than crashing or being retried as a transient provider error.

### Changed

- `--record` refuses to run under the mock provider rather than writing a
  cassette of a cassette.
- The three committed cassettes were backfilled with request hashes by replaying
  their own scripts against the current fixtures. Responses were verified
  identical before and after, so the repo's evals are now drift-checked without
  changing what they assert.
- `SYSTEM_PROMPT` now forbids citing anything but a real tool name as an
  evidence `source`. Free models were citing `"alert"`.
- The eval scorer now fails on **any** fabricated evidence source, not just when
  all sources are invalid.

### Fixed

- The CLI ignored `TRIAGE_MAX_STEPS` and `TRIAGE_MAX_COST_USD`, hardcoding
  `max_steps=12` and the default cost ceiling. Both are now honoured, which is
  also what makes a recorded run and its replay use identical budgets.

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
