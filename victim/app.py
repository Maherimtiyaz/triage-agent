"""The victim service: a tiny HTTP service you also own.

You write this so you can PLANT realistic failures and know the ground truth.
Never build an agent eval against a system whose true failure mode you don't
control — you'll never be able to score the answer.

Stdlib only, so it runs anywhere with zero dependencies.
"""

from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from victim.faults import ACTIVE_FAULT, apply_fault


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        fault = apply_fault(ACTIVE_FAULT)
        try:
            if self.path == "/healthz":
                self._json(200, {"status": "ok" if fault["healthy"] else "degraded"})
            elif self.path == "/metrics":
                self._json(200, fault["metrics"])
            elif self.path == "/":
                # simulate latency/error injection
                if fault["sleep_s"]:
                    time.sleep(fault["sleep_s"])
                if random.random() < fault["error_rate"]:
                    self._json(500, {"error": "internal"})
                else:
                    self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        finally:
            self.log_message("%s %s fault=%s", self.command, self.path, ACTIVE_FAULT)

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[victim] {fmt % args}", flush=True)


def serve(port: int = 8080) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[victim] serving on http://127.0.0.1:{port}  fault={ACTIVE_FAULT}", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    import sys

    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
    while True:
        time.sleep(3600)
