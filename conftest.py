import sys
from pathlib import Path

# Make `src/` and the repo root importable without an editable install.
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
for p in (SRC, ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest  # noqa: E402

from triage_agent.guardrails import Policy  # noqa: E402


@pytest.fixture
def victim_root() -> Path:
    return ROOT / "victim"


@pytest.fixture
def policy(victim_root: Path) -> Policy:
    return Policy(
        allowed_tools=frozenset({"query_logs", "list_deploys", "get_metric", "read_file"}),
        allow_run_cmd=False,
        sandbox_root=victim_root,
    )
