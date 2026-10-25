"""OpenTelemetry SpanExporter that ships spans to Kore.ai AMP."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from .assembler import TraceAssembler
from .client import DEFAULT_BASE_URL, DEFAULT_ENDPOINT, AmpClient, SendResult

log = logging.getLogger("amp_otel")


class AmpSpanExporter(SpanExporter):
    """Drop-in exporter: BatchSpanProcessor(AmpSpanExporter()).

    Reads AMP_API_KEY, AMP_BASE_URL and AMP_INGEST_ENDPOINT when arguments are not given.
    export() never raises into the application; failures are logged and counted.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, endpoint: str | None = None,
                 max_trace_age_s: float = 30.0, default_session_id: str | None = None,
                 session_attributes: dict[str, Any] | None = None, client: AmpClient | None = None,
                 **client_kwargs: Any):
        self.client = client or AmpClient(
            api_key=api_key or os.environ.get("AMP_API_KEY", ""),
            base_url=base_url or os.environ.get("AMP_BASE_URL", DEFAULT_BASE_URL),
            endpoint=endpoint or os.environ.get("AMP_INGEST_ENDPOINT", DEFAULT_ENDPOINT),
            session_attributes=session_attributes,
            **client_kwargs,
        )
        self.assembler = TraceAssembler(max_age_s=max_trace_age_s, default_session_id=default_session_id)
        self.results: list[SendResult] = []
        self.export_calls = 0
        self.dropped_traces = 0
        self._lock = threading.Lock()
        self._shutdown = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        try:
            with self._lock:
                self.export_calls += 1
                self.assembler.add(spans)
                return self._send(self.assembler.drain())
        except Exception:  # never break the instrumented app
            log.exception("amp-otel: export failed")
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            with self._lock:
                return self._send(self.assembler.drain(force=True)) is SpanExportResult.SUCCESS
        except Exception:
            log.exception("amp-otel: flush failed")
            return False

    def shutdown(self) -> None:
        if not self._shutdown:
            self.force_flush()
            self._shutdown = True

    def _send(self, traces: list[dict[str, Any]]) -> SpanExportResult:
        if not traces:
            return SpanExportResult.SUCCESS
        results = self.client.send(traces)
        self.results.extend(results)
        failed = [r for r in results if not r.ok]
        for r in failed:
            self.dropped_traces += r.traces
            log.warning("amp-otel: dropped %d traces (status=%s, %s)", r.traces, r.status, r.error)
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS
