# amp-otel

An OpenTelemetry span exporter that sends traces from Python agents to Kore.ai AMP (Agent Management Platform).

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from amp_otel import AmpSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(AmpSpanExporter()))  # reads AMP_API_KEY
```

Any Python agent that already emits OpenTelemetry spans (OTel GenAI semantic conventions, or OpenInference from LlamaIndex / CrewAI / DSPy / OpenAI Agents instrumentors) can then show up in AMP without being rewritten against a new SDK.

## Why this exists

Kore.ai publishes `@koreaiinc/amp-sdk` for TypeScript. There is no Python SDK on GitHub or PyPI (checked 2026-10-25), and most agent code people want to govern is Python. Those teams already have OTel spans. What they lack is a bridge to AMP's ingest format, and that bridge has 3 traps this package handles:

1. **Split traces.** `BatchSpanProcessor` exports up to 512 spans at a time, in the order they end. One agent turn can land in 2 or 3 `export()` calls. AMP groups spans under a trace with one `session_id` and one status, so a naive exporter sends fragments. `amp-otel` buffers per `trace_id` and sends a trace once its root span has ended (roots end last), or after `max_trace_age_s` (default 30s) for orphans.
2. **Attribute names.** AMP keys on `gen_ai.usage.input_tokens`, `gen_ai.system`, `tool.name`. OpenInference writes `llm.token_count.prompt`, newer OTel semconv writes `gen_ai.provider.name`. Without aliasing, those tokens are invisible to AMP. Aliases only fill a missing key and never overwrite one.
3. **Span types.** AMP wants `llm | tool | rag | agent | orchestration | custom`. The type comes from `gen_ai.operation.name`, then `openinference.span.kind`, then attribute fallbacks. Set `amp.span.type` to force one.

Also: payloads are chunked at AMP's documented 100 traces per batch, 429/5xx/network errors retry with backoff and honor `Retry-After`, 400/401 are not retried, and `export()` never raises into the app (failures are logged and counted in `dropped_traces`).

## Mapping

| OTel | AMP |
|---|---|
| `trace_id` / `span_id` (ints) | 32 / 16 char lowercase hex, same as the TS SDK |
| `start_time` / `end_time` (ns) | ISO 8601 UTC with ms (`2026-10-25T12:19:28.838Z`) |
| parent span | `parent_span_id` (omitted for roots, since the schema types it as string) |
| `StatusCode.ERROR` + description | `status: "error"`, `status_message` |
| span events | `events[]` |
| `session.id` / `gen_ai.conversation.id` on any span or the resource | trace `session_id` |
| root span `amp.trace.input` / `amp.trace.output` (or OpenInference `input.value` / `output.value`) | trace `input` / `output` |
| root span `amp.trace.<key>` | trace `attributes.<key>` |

## Quickstart

```bash
uv sync
uv run pytest -q
uv run python examples/refund_agent_demo.py          # against the local mock
AMP_API_KEY=... uv run python examples/refund_agent_demo.py --real   # against amp.kore.ai
uv run amp-otel-check examples/sample-payload.json   # validate any payload against Kore.ai's OpenAPI file
```

Config: `AMP_API_KEY`, `AMP_BASE_URL` (default `https://amp.kore.ai`), `AMP_INGEST_ENDPOINT` (default `/ingestion/api/v1/telemetry`, the path the TS SDK 1.2.0 uses). Constructor args: `max_trace_age_s`, `default_session_id`, `session_attributes`, `use_gzip`, `max_retries`, `timeout`.

## Demo run

`examples/refund_agent_demo.py` traces a 2-turn refund agent with plain OpenTelemetry. Turn 1 mixes OpenInference and OTel GenAI attributes. Turn 2 has a tool that fails. Batches are set to 3 spans so turns get split, and the mock rate-limits the first request.

```
$ uv run python examples/refund_agent_demo.py
OTel export() calls:            4
(trace, batch) fragments:       5  <- what a per-batch exporter would send as traces
AMP batch: HTTP 202, 1 trace(s), 2 attempt(s)
AMP batch: HTTP 202, 1 trace(s), 1 attempt(s)
dropped traces:                 0
shutdown flush took:            1.06s
HTTP requests seen by mock AMP: 3 -> [429, 202, 202]
traces accepted:                2
  'invoke_agent refund-agent' session=conv-7731 status=ok spans=5 types={'agent': 1, 'llm': 2, 'rag': 1, 'tool': 1} tokens in/out=1717/105
  'invoke_agent refund-agent' session=conv-7731 status=error spans=4 types={'agent': 1, 'llm': 2, 'tool': 1} tokens in/out=2112/67
    error: execute_tool issue_refund: payments API 503: refund not issued
  OpenInference span 'ChatCompletion': llm.token_count.prompt=812 -> gen_ai.usage.input_tokens=812, gen_ai.system=openai
  turn 1 input tokens AMP would count without the alias: 905 of 1717
schema problems in accepted payloads (amp-telemetry-sdk.yaml): 0
wrote sample-payload.json
```

So: 9 spans in 5 batch fragments arrived as 2 whole traces, the 429 was retried after the 1s `Retry-After`, and the failed refund marks turn 2 as `error`. Without the alias, turn 1 would show 905 input tokens instead of 1717. The payload it sent is in `examples/sample-payload.json`.

```
$ uv run pytest -q
.......................                                                  [100%]
23 passed in 3.17s
```

## What is real and what is mocked

- **Mocked:** the AMP server. I don't have an AMP API key, so `amp_otel/mock_server.py` stands in for `/ingestion/api/v1/telemetry`. It checks `X-API-Key`, validates every payload against Kore.ai's published schema, returns the documented `TelemetryResponse` / `ErrorResponse` shapes and can send 429s. It does not know anything about AMP's processing after ingest.
- **Simulated:** the agent in the demo. The spans carry the attributes real instrumentors emit, but no LLM is called.
- **Real:** the OpenTelemetry SDK, `BatchSpanProcessor` batching, the HTTP client, and the schema (`amp_otel/amp-telemetry-sdk.yaml`, copied from `Koredotcom/amp-sdk`). `--real` sends the same demo to `amp.kore.ai` once `AMP_API_KEY` is set. That path is untested by me.

## Things I noticed in amp-sdk

- `openapi/amp-telemetry-sdk.yaml` at `45a7938` ends with a stray `d` line, so it isn't valid YAML (PyYAML: `could not find expected ':'`, line 595). Codegen or validators that load it fail. One-line fix in `upstream/amp-sdk-openapi-stray-char.patch`; the copy bundled here has it applied.
- The spec documents `POST /api/v1/telemetry`, while `constants.ts` sends to `/ingestion/api/v1/telemetry`. This package defaults to the SDK's path and lets you override it. I couldn't check which one production serves.
- `retry()` in `utils/index.ts` retries every failure, including 400 and 401, with a fixed 500ms doubling backoff, and doesn't read `Retry-After`. This package retries only 408/429/5xx/network errors and uses `Retry-After` when present.

## Layout

```
amp_otel/mapping.py      span -> AMP span: type, ids, times, aliases
amp_otel/assembler.py    buffers spans into whole traces
amp_otel/client.py       stdlib HTTP client: chunking, retries, gzip
amp_otel/exporter.py     OTel SpanExporter
amp_otel/validate.py     schema check + amp-otel-check CLI
amp_otel/mock_server.py  local AMP stand-in for tests and the demo
```

MIT. The bundled OpenAPI file is Copyright (c) 2025 Kore.ai, MIT.
