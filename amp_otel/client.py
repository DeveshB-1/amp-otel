"""HTTP client for the AMP telemetry ingest endpoint (stdlib only)."""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "https://amp.kore.ai"
# Path used by @koreaiinc/amp-sdk 1.2.0 (packages/core/src/constants.ts).
# The bundled OpenAPI file documents /api/v1/telemetry instead; both are configurable.
DEFAULT_ENDPOINT = "/ingestion/api/v1/telemetry"
MAX_TRACES_PER_BATCH = 100  # documented AMP limit
USER_AGENT = "amp-otel-python/0.1.0"
RETRYABLE = {408, 429, 500, 502, 503, 504}


@dataclass
class SendResult:
    ok: bool
    status: int | None
    traces: int
    attempts: int
    body: Any = None
    error: str | None = None


class AmpClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, endpoint: str = DEFAULT_ENDPOINT,
                 timeout: float = 30.0, max_retries: int = 3, backoff_s: float = 0.5, max_delay_s: float = 30.0,
                 use_gzip: bool = False, session_attributes: dict[str, Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, urlopen: Callable[..., Any] = urllib.request.urlopen):
        if not api_key:
            raise ValueError("AMP api_key is required (AMP dashboard: Settings > API Keys)")
        self.api_key = api_key
        self.url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_delay_s = max_delay_s
        self.use_gzip = use_gzip
        self.session_attributes = dict(session_attributes or {})
        self._sleep = sleep
        self._urlopen = urlopen

    def send(self, traces: list[dict[str, Any]]) -> list[SendResult]:
        """Send traces in chunks of at most 100. Returns one result per HTTP batch."""
        results = []
        for i in range(0, len(traces), MAX_TRACES_PER_BATCH):
            chunk = traces[i:i + MAX_TRACES_PER_BATCH]
            payload: dict[str, Any] = {"traces": chunk}
            if self.session_attributes:
                payload = {"attributes": self.session_attributes, **payload}
            results.append(self._post(payload, len(chunk)))
        return results

    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key, "User-Agent": USER_AGENT}
        if self.use_gzip:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return urllib.request.Request(self.url, data=body, headers=headers, method="POST")

    def _post(self, payload: dict[str, Any], n: int) -> SendResult:
        attempt = 0
        while True:
            attempt += 1
            status, body, retry_after, error = None, None, None, None
            try:
                with self._urlopen(self._request(payload), timeout=self.timeout) as resp:
                    raw = resp.read()
                    return SendResult(True, resp.status, n, attempt, _parse(raw))
            except urllib.error.HTTPError as e:
                status = e.code
                body = _parse(e.read() or b"")
                retry_after = _retry_after(e.headers.get("Retry-After") if e.headers else None, body)
                error = (body or {}).get("message") if isinstance(body, dict) else f"HTTP {e.code}"
                retryable = status in RETRYABLE
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                error = str(getattr(e, "reason", e))
                retryable = True
            if not retryable or attempt > self.max_retries:
                return SendResult(False, status, n, attempt, body, error)
            delay = retry_after if retry_after is not None else self.backoff_s * 2 ** (attempt - 1)
            self._sleep(min(delay, self.max_delay_s))


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode() or "null")
    except (ValueError, UnicodeDecodeError):
        return raw.decode(errors="replace")


def _retry_after(header: str | None, body: Any) -> float | None:
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    if isinstance(body, dict) and isinstance(body.get("retryAfter"), (int, float)):
        return float(body["retryAfter"])
    return None
