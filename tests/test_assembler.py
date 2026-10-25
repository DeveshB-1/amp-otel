from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from amp_otel.assembler import TraceAssembler


def turn(tracer, conv="c1", fail=False):
    with tracer.start_as_current_span("invoke_agent", attributes={
            "gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conv,
            "amp.trace.input": "hi", "amp.trace.output": "hello", "amp.trace.experiment_group": "A"}):
        with tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"}):
            pass
        with tracer.start_as_current_span("tool", attributes={"gen_ai.operation.name": "execute_tool"}) as s:
            if fail:
                s.set_status(Status(StatusCode.ERROR, "x"))


def spans(*turns):
    mem = InMemorySpanExporter()
    p = TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(mem))
    t = p.get_tracer("t")
    for kw in turns:
        turn(t, **kw)
    p.shutdown()
    return list(mem.get_finished_spans())


def test_trace_held_until_root_arrives_across_batches():
    s = spans({})  # order: chat, tool, root
    asm = TraceAssembler()
    asm.add(s[:1])
    assert asm.drain() == []
    asm.add(s[1:2])
    assert asm.drain() == []
    asm.add(s[2:])
    [trace] = asm.drain()
    assert len(trace["spans"]) == 3
    assert trace["session_id"] == "c1"
    assert trace["trace_name"] == "invoke_agent"
    assert trace["status"] == "ok"
    assert trace["input"] == {"role": "human", "content": "hi"}
    assert trace["output"] == {"role": "AI", "content": "hello"}
    assert trace["attributes"] == {"experiment_group": "A"}
    assert trace["start_time"] <= trace["end_time"]
    assert len(asm) == 0


def test_error_in_any_span_marks_trace_error():
    [trace] = (lambda a: (a.add(spans({"fail": True})), a.drain())[1])(TraceAssembler())
    assert trace["status"] == "error"


def test_two_turns_two_traces_same_session():
    asm = TraceAssembler()
    asm.add(spans({}, {}))
    traces = asm.drain()
    assert len(traces) == 2
    assert {t["session_id"] for t in traces} == {"c1"}
    assert traces[0]["trace_id"] != traces[1]["trace_id"]


def test_orphans_released_after_max_age_or_force():
    now = [0.0]
    s = spans({})
    asm = TraceAssembler(max_age_s=10, clock=lambda: now[0])
    asm.add(s[:2])  # root never arrives
    assert asm.drain() == []
    now[0] = 11
    [trace] = asm.drain()
    assert trace["metadata"] == {"amp_otel.partial": True}
    asm.add(s[:1])
    assert len(asm.drain(force=True)) == 1


def test_session_fallbacks():
    asm = TraceAssembler(default_session_id="sess-default")
    mem = InMemorySpanExporter()
    p = TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(mem))
    with p.get_tracer("t").start_as_current_span("plain"):
        pass
    asm.add(mem.get_finished_spans())
    assert asm.drain()[0]["session_id"] == "sess-default"
    asm2 = TraceAssembler()
    asm2.add(mem.get_finished_spans())
    t = asm2.drain()[0]
    assert t["session_id"] == "sess_" + t["trace_id"][:16]
