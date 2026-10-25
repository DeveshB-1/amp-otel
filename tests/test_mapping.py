from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from amp_otel.mapping import apply_aliases, classify, ns_to_iso, span_to_amp


def make_spans(fn):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mem))
    fn(provider.get_tracer("t"))
    provider.shutdown()
    return mem.get_finished_spans()


def test_classify_genai_semconv_operations():
    assert classify({"gen_ai.operation.name": "chat"}) == "llm"
    assert classify({"gen_ai.operation.name": "embeddings"}) == "llm"
    assert classify({"gen_ai.operation.name": "execute_tool"}) == "tool"
    assert classify({"gen_ai.operation.name": "invoke_agent"}) == "agent"
    assert classify({"gen_ai.operation.name": "retrieval"}) == "rag"


def test_classify_openinference_kinds():
    assert classify({"openinference.span.kind": "LLM"}) == "llm"
    assert classify({"openinference.span.kind": "RETRIEVER"}) == "rag"
    assert classify({"openinference.span.kind": "TOOL"}) == "tool"
    assert classify({"openinference.span.kind": "CHAIN"}) == "orchestration"


def test_classify_fallbacks_and_override():
    assert classify({"gen_ai.request.model": "gpt-4o"}) == "llm"
    assert classify({"gen_ai.tool.name": "x"}) == "tool"
    assert classify({"rag.db_system": "qdrant"}) == "rag"
    assert classify({"http.method": "GET"}) == "custom"
    assert classify({"gen_ai.operation.name": "chat", "amp.span.type": "orchestration"}) == "orchestration"
    assert classify({"amp.span.type": "bogus", "gen_ai.operation.name": "chat"}) == "llm"


def test_aliases_fill_amp_keys_without_overwriting():
    attrs = {"llm.token_count.prompt": 10, "llm.token_count.completion": 3, "llm.model_name": "m",
             "gen_ai.provider.name": "anthropic", "gen_ai.tool.name": "search"}
    added = apply_aliases(attrs)
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.request.model"] == "m"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["tool.name"] == "search"
    assert set(added) >= {"gen_ai.usage.input_tokens", "gen_ai.system"}
    keep = {"gen_ai.usage.input_tokens": 99, "gen_ai.usage.prompt_tokens": 5}
    apply_aliases(keep)
    assert keep["gen_ai.usage.input_tokens"] == 99


def test_ns_to_iso_matches_js_toisostring():
    assert ns_to_iso(1735120800_123_456_789) == "2024-12-25T10:00:00.123Z"
    assert ns_to_iso(None) is None


def test_span_to_amp_ids_times_parent_status_events():
    def run(tracer):
        with tracer.start_as_current_span("root"):
            with tracer.start_as_current_span("tool", attributes={"gen_ai.operation.name": "execute_tool",
                                                                  "tags": ("a", "b")}) as s:
                s.add_event("gen_ai.content.prompt", {"x": 1})
                s.set_status(Status(StatusCode.ERROR, "boom"))

    child, root = make_spans(run)
    c, r = span_to_amp(child), span_to_amp(root)
    assert len(c["trace_id"]) == 32 and len(c["span_id"]) == 16
    assert c["trace_id"] == r["trace_id"]
    assert c["parent_span_id"] == r["span_id"]
    assert "parent_span_id" not in r  # omitted, not null: the schema types it as string
    assert c["type"] == "tool" and c["status"] == "error" and c["status_message"] == "boom"
    assert c["attributes"]["tags"] == ["a", "b"]
    assert c["events"][0]["name"] == "gen_ai.content.prompt"
    assert c["start_time"].endswith("Z") and c["start_time"] <= c["end_time"]
    assert r["status"] == "unset"
