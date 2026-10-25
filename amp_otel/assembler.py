"""Group exported spans into whole AMP traces.

OpenTelemetry's BatchSpanProcessor exports spans in batches of up to 512 as they
end, so one agent turn's spans can arrive over several export() calls. AMP's
payload groups spans under a trace with one session_id and one status, so
sending each batch as-is would split a turn into partial traces. The assembler
buffers spans per trace_id and releases a trace once its local root span has
ended (roots end last), or once it has waited longer than max_age_s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .mapping import is_local_root, session_id_for, span_to_amp


@dataclass
class _Pending:
    spans: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    root: dict[str, Any] | None = None
    root_attrs: dict[str, Any] = field(default_factory=dict)
    first_seen: float = 0.0


class TraceAssembler:
    def __init__(self, max_age_s: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 default_session_id: str | None = None):
        self.max_age_s = max_age_s
        self.clock = clock
        self.default_session_id = default_session_id
        self._pending: dict[str, _Pending] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def add(self, otel_spans) -> None:
        now = self.clock()
        for span in otel_spans:
            amp = span_to_amp(span)
            p = self._pending.get(amp["trace_id"])
            if p is None:
                p = self._pending[amp["trace_id"]] = _Pending(first_seen=now)
            p.spans.append(amp)
            if p.session_id is None:
                p.session_id = session_id_for(span)
            if is_local_root(span):
                p.root = amp
                p.root_attrs = dict(span.attributes or {})

    def drain(self, force: bool = False) -> list[dict[str, Any]]:
        """Return traces that are complete (root seen), too old, or all of them when force=True."""
        now = self.clock()
        ready = [tid for tid, p in self._pending.items()
                 if force or p.root is not None or now - p.first_seen >= self.max_age_s]
        return [self._build(tid, self._pending.pop(tid)) for tid in ready]

    def _build(self, trace_id: str, p: _Pending) -> dict[str, Any]:
        starts = [s["start_time"] for s in p.spans if s.get("start_time")]
        ends = [s["end_time"] for s in p.spans if s.get("end_time")]
        has_error = any(s["status"] == "error" for s in p.spans)
        trace: dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": p.session_id or self.default_session_id or f"sess_{trace_id[:16]}",
            "status": "error" if has_error else "ok",
            "spans": p.spans,
        }
        if starts:
            trace["start_time"] = min(starts)
        if ends:
            trace["end_time"] = max(ends)
        if p.root is not None:
            trace["trace_name"] = p.root["name"]
            attrs = p.root_attrs
            trace_attrs = {k[len("amp.trace."):]: v for k, v in attrs.items()
                           if k.startswith("amp.trace.") and k not in ("amp.trace.input", "amp.trace.output")
                           and isinstance(v, (str, int, float, bool))}
            if trace_attrs:
                trace["attributes"] = trace_attrs
            user_in = attrs.get("amp.trace.input") or attrs.get("input.value")
            ai_out = attrs.get("amp.trace.output") or attrs.get("output.value")
            if user_in:
                trace["input"] = {"role": "human", "content": str(user_in)}
            if ai_out:
                trace["output"] = {"role": "AI", "content": str(ai_out)}
        else:
            trace["metadata"] = {"amp_otel.partial": True}
        return trace
