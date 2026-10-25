import gzip
import json
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult

from amp_otel import AmpClient, AmpSpanExporter
from amp_otel.mock_server import MockAmp
from amp_otel.validate import SPEC_PATH, main as check_main, validate_payload


def trace(i):
    return {"trace_id": f"{i:032x}", "session_id": "s", "spans": [{"span_id": f"{i:016x}", "trace_id": f"{i:032x}", "name": "x"}]}


def test_spec_examples_validate():
    spec = yaml.safe_load(SPEC_PATH.read_text())
    examples = spec["paths"]["/api/v1/telemetry"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    for ex in examples.values():
        assert validate_payload(ex["value"]) == []


def test_validator_catches_bad_payloads():
    assert any("no traces" in e for e in validate_payload({}))
    bad = {"traces": [{"trace_id": "t", "spans": []}]}
    errs = validate_payload(bad)
    assert any("session_id" in e for e in errs)
    assert any("spans" in e for e in errs)
    wrong_type = {"traces": [{"trace_id": "t", "session_id": "s", "spans": [
        {"span_id": "a", "trace_id": "other", "name": "n", "type": "llmm", "start_time": "yesterday"}]}]}
    errs = validate_payload(wrong_type)
    assert any("llmm" in e for e in errs)
    assert any("not ISO 8601" in e for e in errs)
    assert any("does not match parent trace" in e for e in errs)
    too_many = {"traces": [trace(i) for i in range(101)]}
    assert any("too long" in e or "101" in e for e in validate_payload(too_many))


def test_client_chunks_at_100_and_sends_api_key():
    with MockAmp(api_key="k") as mock:
        client = AmpClient("k", base_url=mock.base_url)
        results = client.send([trace(i) for i in range(250)])
    assert [r.traces for r in results] == [100, 100, 50]
    assert all(r.ok and r.status == 202 for r in results)
    assert mock.requests[0]["path"] == "/ingestion/api/v1/telemetry"
    assert mock.requests[0]["headers"]["x-api-key"] == "k"
    assert results[0].body["status"] == "accepted"


def test_retry_honors_retry_after_on_429():
    sleeps = []
    with MockAmp(api_key="k", fail_first=2, retry_after=7) as mock:
        [r] = AmpClient("k", base_url=mock.base_url, sleep=sleeps.append).send([trace(1)])
    assert r.ok and r.attempts == 3
    assert sleeps == [7.0, 7.0]


def test_no_retry_on_401_or_400():
    sleeps = []
    with MockAmp(api_key="right") as mock:
        [r] = AmpClient("wrong", base_url=mock.base_url, sleep=sleeps.append).send([trace(1)])
        assert not r.ok and r.status == 401 and r.attempts == 1 and r.error == "Invalid API key"
        bad = {"trace_id": "t", "session_id": "s", "spans": []}
        [r] = AmpClient("right", base_url=mock.base_url, sleep=sleeps.append).send([bad])
        assert r.status == 400 and r.attempts == 1 and r.body["error"] == "VALIDATION_ERROR"
    assert sleeps == []


def test_network_error_retries_with_backoff_then_gives_up():
    sleeps = []
    client = AmpClient("k", base_url="http://127.0.0.1:9", max_retries=2, backoff_s=0.5, sleep=sleeps.append, timeout=1)
    [r] = client.send([trace(1)])
    assert not r.ok and r.attempts == 3 and sleeps == [0.5, 1.0]


def test_gzip_body():
    with MockAmp(api_key="k") as mock:
        [r] = AmpClient("k", base_url=mock.base_url, use_gzip=True).send([trace(1)])
    assert r.ok and mock.requests[0]["headers"]["content-encoding"] == "gzip"


def test_session_attributes_go_to_payload_level():
    with MockAmp(api_key="k") as mock:
        AmpClient("k", base_url=mock.base_url, session_attributes={"region": "in"}).send([trace(1)])
    assert mock.accepted[0]["attributes"] == {"region": "in"}


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        AmpSpanExporter()


def test_exporter_end_to_end_with_batch_processor():
    with MockAmp(api_key="k") as mock:
        exp = AmpSpanExporter(api_key="k", base_url=mock.base_url)
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(exp, max_export_batch_size=2, schedule_delay_millis=10))
        tracer = provider.get_tracer("t")
        for conv in ("a", "b", "c"):
            with tracer.start_as_current_span("turn", attributes={"session.id": conv, "gen_ai.operation.name": "invoke_agent"}):
                for i in range(3):
                    with tracer.start_as_current_span(f"chat {i}", attributes={"gen_ai.operation.name": "chat"}):
                        pass
        provider.shutdown()
    traces = [t for p in mock.accepted for t in p["traces"]]
    assert sorted(t["session_id"] for t in traces) == ["a", "b", "c"]
    assert all(len(t["spans"]) == 4 for t in traces)  # never split across export() calls
    assert exp.export_calls >= 6 and exp.dropped_traces == 0
    assert all(validate_payload(p) == [] for p in mock.accepted)


def test_exporter_never_raises_when_amp_is_down():
    exp = AmpSpanExporter(api_key="k", base_url="http://127.0.0.1:9", max_retries=0, timeout=1)
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    provider.add_span_processor(SimpleSpanProcessor(exp))
    with provider.get_tracer("t").start_as_current_span("x"):
        pass
    provider.shutdown()
    assert exp.dropped_traces == 1
    assert exp.export([]) is SpanExportResult.FAILURE  # after shutdown


def test_check_cli(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"traces": [trace(1)]}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"traces": [{"trace_id": "t"}]}))
    assert check_main([str(good)]) == 0
    assert check_main([str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "OK   " in out and "FAIL " in out
