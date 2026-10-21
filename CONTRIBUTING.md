# Contributing

Thanks for looking. This repo is a teaching harness, so contributions that make
a production lesson *clearer* are as welcome as features.

## Setup

```bash
git clone https://github.com/OWNER/triage-agent.git
cd triage-agent
py -m venv .venv
.\.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

No API key is required: the default provider is a recorded mock, so the full
suite runs offline and for free.

## Verify before you push

```bash
ruff check .        # must pass
pytest -q           # 35 tests must pass
python evals/runner.py   # must be 100%
```

CI enforces exactly these three, plus the eval gate.

## The one rule

**Do not change agent behaviour without a test or an eval that would catch a
regression.** Prompts, tool descriptions, and the system prompt are code. If you
tweak `SYSTEM_PROMPT`, the recorded cassettes in `evals/cassettes/` will go
stale and the eval will tell you — that is the point, not a nuisance.

## Common changes

### Add a tool

1. Define a `ToolSpec` in `tools.py` (name, description, JSON Schema, timeout,
   result cap). The description **is** part of the prompt — be specific.
2. Implement it and register it in `build_registry`.
3. Add its name to the default `TRIAGE_ALLOWED_TOOLS` in `config.py`.
4. Add a test in `tests/test_tools.py`, including its failure path.

### Add an incident scenario

1. Plant the fault in `victim/faults.py`.
2. Add a fixture in `victim/fixtures/<name>.json` (logs, deploys, metrics that
   actually corroborate the fault).
3. Record a cassette in `evals/cassettes/<name>.json`.
4. Add a `Case(...)` row to `DATASET` in `evals/runner.py` with `must_match`
   keywords drawn from the fault's `ground_truth`.

That four-step loop is exactly what you would run in production when someone
reports a new failure mode.

### Add a provider

Implement the `Provider` protocol in `providers.py` (one method: `complete`).
Keep vendor SDK imports lazy so the base install stays dependency-light. Add it
to `build_provider`.

## Commit style

Conventional Commits, scoped to the subsystem:
`feat(tools):`, `fix(loop):`, `test(evals):`, `docs(readme):`, `chore(ci):`.

Explain **why** in the body, not what — the diff already shows what.
