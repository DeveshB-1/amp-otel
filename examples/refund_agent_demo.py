"""Demo: a 2-turn refund agent traced with plain OpenTelemetry, exported to AMP.

The agent is simulated (no LLM key needed): the spans carry the attributes that
real instrumentors emit, both OTel GenAI semconv (gen_ai.*) and OpenInference
(llm.token_count.*, openinference.span.kind). The AMP endpoint is the local mock
built from Kore.ai's OpenAPI file, set to rate-limit the first request.

    uv run python examples/refund_agent_demo.py            # mock AMP
    AMP_API_KEY=... uv run python examples/refund_agent_demo.py --real   # amp.kore.ai
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from amp_otel import AmpSpanExporter  # noqa: E402
from amp_otel.mock_server import MockAmp  # noqa: E402
from amp_otel.validate import validate_payload  # noqa: E402

CONVERSATION = "conv-7731"


class RecordingExporter(AmpSpanExporter):
    """Remembers which trace ids each export() call carried, to show the batch split."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.batches: list[set[str]] = []

    def export(self, spans):
        self.batches.append({format(s.context.trace_id, "032x") for s in spans})
        return super().export(spans)


def run_agent(tracer) -> None:
    # Turn 1: "where is my order?"  -> retrieval, LLM (OpenInference attrs), tool, LLM
    with tracer.start_as_current_span("invoke_agent refund-agent", attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "refund-agent",
        "gen_ai.conversation.id": CONVERSATION, "amp.trace.input": "Where is order 4417?",
        "amp.trace.output": "Order 4417 shipped on 21 Oct and arrives tomorrow.",
        "amp.trace.channel": "web"}):
        with tracer.start_as_current_span("retrieve policy_kb", attributes={
                "openinference.span.kind": "RETRIEVER", "rag.db_system": "qdrant",
                "rag.documents_retrieved": 4}):
            time.sleep(0.01)
        with tracer.start_as_current_span("ChatCompletion", attributes={
                "openinference.span.kind": "LLM", "llm.provider": "openai", "llm.model_name": "gpt-4o-mini",
                "llm.token_count.prompt": 812, "llm.token_count.completion": 64}):
            time.sleep(0.01)
        with tracer.start_as_current_span("execute_tool lookup_order", attributes={
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
                "gen_ai.tool.call.id": "call_01"}):
            time.sleep(0.01)
        with tracer.start_as_current_span("chat gpt-4o-mini", attributes={
                "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
                "gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 905,
                "gen_ai.usage.output_tokens": 41}):
            time.sleep(0.01)

    # Turn 2: "refund it" -> LLM, failing tool, LLM apology
    with tracer.start_as_current_span("invoke_agent refund-agent", attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "refund-agent",
        "gen_ai.conversation.id": CONVERSATION, "amp.trace.input": "Cancel it and refund me.",
        "amp.trace.output": "I couldn't issue the refund. A human agent will follow up."}):
        with tracer.start_as_current_span("chat gpt-4o-mini", attributes={
                "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
                "gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 1010,
                "gen_ai.usage.output_tokens": 38}):
            time.sleep(0.01)
        with tracer.start_as_current_span("execute_tool issue_refund", attributes={
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "issue_refund",
                "gen_ai.tool.call.id": "call_02"}) as span:
            span.set_status(Status(StatusCode.ERROR, "payments API 503: refund not issued"))
        with tracer.start_as_current_span("chat gpt-4o-mini", attributes={
                "gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
                "gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 1102,
                "gen_ai.usage.output_tokens": 29}):
            time.sleep(0.01)


def main() -> int:
    real = "--real" in sys.argv
    mock = None if real else MockAmp(api_key="demo-key", fail_first=1, retry_after=1).start()
    exporter = RecordingExporter(api_key=None if real else "demo-key",
                                 base_url=None if real else mock.base_url)
    provider = TracerProvider(resource=Resource.create({"service.name": "refund-agent"}))
    # Small batches on purpose: spans of one turn land in different export() calls.
    provider.add_span_processor(BatchSpanProcessor(exporter, max_export_batch_size=3, schedule_delay_millis=20))
    run_agent(provider.get_tracer("demo"))
    t0 = time.perf_counter()
    provider.shutdown()
    elapsed = time.perf_counter() - t0

    print(f"OTel export() calls:            {exporter.export_calls}")
    partial = sum(len(b) for b in exporter.batches)
    print(f"(trace, batch) fragments:       {partial}  <- what a per-batch exporter would send as traces")
    for r in exporter.results:
        print(f"AMP batch: HTTP {r.status}, {r.traces} trace(s), {r.attempts} attempt(s)")
    print(f"dropped traces:                 {exporter.dropped_traces}")
    print(f"shutdown flush took:            {elapsed:.2f}s")
    if real:
        return 0 if exporter.dropped_traces == 0 else 1

    codes = [429 if i < mock.fail_first else 202 for i in range(len(mock.requests))]
    print(f"HTTP requests seen by mock AMP: {len(mock.requests)} -> {codes}")
    traces = [t for p in mock.accepted for t in p["traces"]]
    print(f"traces accepted:                {len(traces)}")
    for t in traces:
        types = Counter(s["type"] for s in t["spans"])
        tin = sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in t["spans"])
        tout = sum(s["attributes"].get("gen_ai.usage.output_tokens", 0) for s in t["spans"])
        errs = [f'{s["name"]}: {s.get("status_message")}' for s in t["spans"] if s["status"] == "error"]
        print(f"  {t['trace_name']!r} session={t['session_id']} status={t['status']} spans={len(t['spans'])} "
              f"types={dict(sorted(types.items()))} tokens in/out={tin}/{tout}")
        if errs:
            print(f"    error: {errs[0]}")
    llm = [s for t in traces for s in t["spans"] if "llm.token_count.prompt" in s["attributes"]]
    for s in llm:
        a = s["attributes"]
        print(f"  OpenInference span {s['name']!r}: llm.token_count.prompt={a['llm.token_count.prompt']} "
              f"-> gen_ai.usage.input_tokens={a['gen_ai.usage.input_tokens']}, gen_ai.system={a['gen_ai.system']}")
    t1 = traces[0]["spans"]
    raw = sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in t1
              if "llm.token_count.prompt" not in s["attributes"])
    full = sum(s["attributes"].get("gen_ai.usage.input_tokens", 0) for s in t1)
    print(f"  turn 1 input tokens AMP would count without the alias: {raw} of {full}")
    problems = sum(len(validate_payload(p)) for p in mock.accepted)
    print(f"schema problems in accepted payloads (amp-telemetry-sdk.yaml): {problems}")
    out = Path(__file__).with_name("sample-payload.json")
    out.write_text(json.dumps(mock.accepted[0], indent=2) + "\n")
    print(f"wrote {out.name}")
    mock.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
