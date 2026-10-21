# Security Policy

## Reporting a vulnerability

Open a private security advisory via GitHub
(**Security → Advisories → Report a vulnerability**) rather than a public issue.

Please include: affected version, a minimal reproduction, and impact. We aim to
acknowledge within 72 hours.

## Scope note for users of this project

This project is an **agent harness**, and its whole point is that the model is
untrusted. Two things deserve your attention before you point it at anything
real:

### 1. The sandbox is by design, not by accident

- `read_file` is confined to a single root directory and resolves `..` and
  symlinks before checking containment (`guardrails.py::Policy::resolve_path`).
  Do **not** replace `Path.resolve()` with a string `startswith()` check — see
  `tests/test_guardrails.py::test_symlink_style_prefix_is_not_enough`.
- `run_cmd` is **disabled by default**, requires an explicit
  `TRIAGE_ALLOW_RUN_CMD=1`, uses an argv allowlist, and never invokes a shell.
  Enabling it against a host you care about is your risk decision.
- Tools are **deny-by-default**: a tool not in `TRIAGE_ALLOWED_TOOLS` cannot run,
  and the policy check never reads model output.

### 2. Prompt injection is in scope

Tool results (logs, files, metrics) are attacker-controllable in a real
incident. A malicious log line can contain instructions. The harness mitigates
this structurally — tool output is truncated, never `eval`'d, and only ever
reaches the model as data — but you should assume a determined injection can
still influence the report.

**Therefore: this agent produces a `suggested_fix`, not an action.** Nothing in
this project executes remediation. If you wire the report into something that
does, put a human approval step in front of it.

### 3. Secrets

- API keys are read from the environment only. Never commit `.env`.
- Traces can contain tool output. Treat `trace.dump()` output as potentially
  sensitive and redact before sharing.
