"""Structured output: turn free text from the model into a validated object.

Production lesson: NEVER parse model output with a regex and hope. The model's
final answer is untrusted input. Validate it against a schema, and on failure,
retry — feeding the exact validation error back so the model can self-correct.

This pattern (schema + bounded retry-with-error-feedback) is what turns "the
agent usually gives good answers" into "the agent either gives a conforming
answer or reports that it could not".
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Severity = Literal["SEV1", "SEV2", "SEV3", "SEV4"]


class Evidence(BaseModel):
    """One piece of grounding. Force the model to cite WHAT it saw."""

    source: str = Field(description="tool or file the evidence came from")
    detail: str = Field(description="the specific observation")


class IncidentReport(BaseModel):
    """The terminal artifact of a triage run."""

    severity: Severity
    summary: str = Field(min_length=10, max_length=500)
    root_cause: str = Field(
        min_length=5,
        max_length=500,
        description="the single most likely cause, stated plainly",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(min_length=1)
    suggested_fix: str = Field(min_length=5, max_length=500)
    suspected_deploy: str | None = Field(
        default=None, description="version string if a deploy is implicated"
    )


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of model prose.

    The model will wrap JSON in ```json fences, prefix it with 'Here is the
    report:', etc. Be permissive about the wrapper, strict about the schema.
    """
    if match := _JSON_BLOCK.search(text):
        return json.loads(match.group(1))
    # fall back to the outermost braces
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in model output")


def parse_report(text: str) -> tuple[IncidentReport | None, str | None]:
    """Return (report, error). Exactly one is non-None."""
    try:
        raw = extract_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"could not parse JSON: {exc}"
    try:
        return IncidentReport.model_validate(raw), None
    except ValidationError as exc:
        # Compact, actionable feedback — the model fixes this reliably when the
        # message names the field and the constraint that failed.
        return None, f"schema validation failed:\n{_compact_errors(exc)}"


def _compact_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


RETRY_PROMPT = """Your previous response was not valid. Fix it and respond with
ONLY the JSON object.

Problem:
{error}

Required shape:
{shape}
"""


def retry_message(error: str) -> str:
    shape = json.dumps(IncidentReport.model_json_schema(), indent=2)
    return RETRY_PROMPT.format(error=error, shape=shape)
