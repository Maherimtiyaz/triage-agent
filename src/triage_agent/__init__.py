"""triage_agent — a production-style harness for one task: incident triage."""

from triage_agent.loop import AgentResult, run
from triage_agent.types import Budget

__all__ = ["run", "Budget", "AgentResult"]
