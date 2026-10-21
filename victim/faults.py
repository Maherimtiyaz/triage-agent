"""Planted faults. Ground truth lives here.

Each fault is a named, reproducible way to break the victim. The eval suite
runs one fault, invokes the agent, and checks whether the agent's `root_cause`
names the same thing this file knows to be true.
"""

from __future__ import annotations

import os

ACTIVE_FAULT = os.environ.get("VICTIM_FAULT", "none")

FAULTS: dict[str, dict] = {
    "none": {"healthy": True, "sleep_s": 0.0, "error_rate": 0.0, "metrics": {}},
    "bad_deploy": {
        "healthy": False, "sleep_s": 0.0, "error_rate": 0.35,
        "ground_truth": "deploy v2.4.1 introduced a regression",
        "metrics": {"error_rate_pct": 35.0, "request_latency_p99_ms": 120.0},
    },
    "slow_query": {
        "healthy": False, "sleep_s": 0.0, "error_rate": 0.02,
        "ground_truth": "database connection pool exhaustion from a slow query",
        "metrics": {"db_connections": 100.0, "request_latency_p99_ms": 2400.0},
    },
    "disk_full": {
        "healthy": False, "sleep_s": 0.0, "error_rate": 0.05,
        "ground_truth": "disk full on the primary node",
        "metrics": {"disk_pct": 99.4},
    },
    "cert_expired": {
        "healthy": False, "sleep_s": 0.0, "error_rate": 0.10,
        "ground_truth": "TLS certificate expired",
        "metrics": {"error_rate_pct": 10.0},
    },
}


def apply_fault(name: str) -> dict:
    return FAULTS.get(name, FAULTS["none"])
