"""Local stand-in for the AMP ingest API, built from Kore.ai's OpenAPI file.

It is a mock: it checks X-API-Key, validates each payload against the published
schema, answers with the documented TelemetryResponse / ErrorResponse shapes,
and can return 429 for the first N requests to exercise retries. It does not
reproduce any server-side processing AMP does after ingest.
"""

from __future__ import annotations

import argparse
import gzip
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .validate import validate_payload

INGEST_PATHS = {"/ingestion/api/v1/telemetry", "/api/v1/telemetry"}


class MockAmp:
    def __init__(self, api_key: str = "test-key", port: int = 0, fail_first: int = 0, retry_after: int = 1):
        self.api_key = api_key
        self.fail_first = fail_first
        self.retry_after = retry_after
        self.requests: list[dict[str, Any]] = []   # every request, accepted or not
        self.accepted: list[dict[str, Any]] = []   # payloads answered 202
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockAmp":
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "MockAmp":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _reply(self, code: int, body: dict[str, Any], headers: dict[str, str] | None = None):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/v1/health":
                    return self._reply(200, {"status": "healthy",
                                             "timestamp": datetime.now(timezone.utc).isoformat(),
                                             "version": "mock"})
                self._reply(404, {"error": "NOT_FOUND", "message": self.path, "statusCode": 404})

            def do_POST(self):
                t0 = time.perf_counter()
                path = self.path.split("?")[0]
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                size = len(raw)
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                try:
                    payload = json.loads(raw or b"null")
                except ValueError:
                    payload = None
                with mock._lock:
                    mock.requests.append({"path": path, "headers": {k.lower(): v for k, v in self.headers.items()}, "payload": payload})
                    n = len(mock.requests)
                if path not in INGEST_PATHS:
                    return self._reply(404, {"error": "NOT_FOUND", "message": path, "statusCode": 404})
                if self.headers.get("X-API-Key") != mock.api_key:
                    return self._reply(401, {"error": "UNAUTHORIZED", "message": "Invalid API key", "statusCode": 401})
                if n <= mock.fail_first:
                    return self._reply(429, {"error": "RATE_LIMIT_EXCEEDED",
                                             "message": f"Too many requests. Retry after {mock.retry_after} seconds.",
                                             "statusCode": 429, "retryAfter": mock.retry_after},
                                       {"Retry-After": str(mock.retry_after)})
                errors = validate_payload(payload)
                if errors:
                    return self._reply(400, {"error": "VALIDATION_ERROR", "message": errors[0],
                                             "statusCode": 400, "details": {"errors": errors}})
                with mock._lock:
                    mock.accepted.append(payload)
                self._reply(202, {"status": "accepted", "ingestion_id": str(uuid.uuid4()), "format": "otel",
                                  "payload_size": size, "duration_ms": int((time.perf_counter() - t0) * 1000),
                                  "persisted": True, "queued": True, "warnings": []})

        return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a local mock of the AMP ingest API")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default="test-key")
    ap.add_argument("--fail-first", type=int, default=0)
    args = ap.parse_args()
    mock = MockAmp(args.api_key, args.port, args.fail_first)
    print(f"mock AMP on {mock.base_url} (key={args.api_key})")
    try:
        mock.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
