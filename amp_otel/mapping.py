"""Map OpenTelemetry spans to the AMP telemetry wire format.

The target format is the one Kore.ai's TypeScript SDK (@koreaiinc/amp-sdk) sends
and that openapi/amp-telemetry-sdk.yaml describes: traces grouped by trace_id,
each carrying a session_id and a list of typed spans (llm, tool, rag, agent,
orchestration, custom).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

SPAN_TYPES = ("llm", "tool", "rag", "agent", "orchestration", "custom")

# Explicit override: set this attribute on a span to force its AMP type.
TYPE_OVERRIDE_KEY = "amp.span.type"

# OTel GenAI semantic conventions: gen_ai.operation.name values.
_LLM_OPS = {"chat", "text_completion", "generate_content", "completion", "embeddings"}
_TOOL_OPS = {"execute_tool"}
_AGENT_OPS = {"invoke_agent", "create_agent"}
_RAG_OPS = {"retrieval", "retrieve", "vector_search", "search"}
_ORCH_OPS = {"workflow", "invoke_workflow", "chain"}

# OpenInference (Arize) span kinds, used by LlamaIndex, CrewAI, DSPy,
# OpenAI Agents and other instrumentors.
_OPENINFERENCE_KIND = {
    "LLM": "llm",
    "EMBEDDING": "llm",
    "TOOL": "tool",
    "RETRIEVER": "rag",
    "RERANKER": "rag",
    "AGENT": "agent",
    "CHAIN": "orchestration",
    "GUARDRAIL": "custom",
    "EVALUATOR": "custom",
}

# AMP keys its dashboards on these names. Each target is filled from the first
# source key that is present, and only when the target itself is missing.
ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "gen_ai.system": ("gen_ai.provider.name", "llm.provider", "llm.system"),
    "gen_ai.request.model": ("gen_ai.response.model", "llm.model_name"),
    "gen_ai.usage.input_tokens": ("gen_ai.usage.prompt_tokens", "llm.token_count.prompt"),
    "gen_ai.usage.output_tokens": ("gen_ai.usage.completion_tokens", "llm.token_count.completion"),
    "tool.name": ("gen_ai.tool.name",),
    "gen_ai.agent.name": ("agent.name",),
}

SESSION_KEYS = ("session.id", "gen_ai.conversation.id", "amp.session.id")


def classify(attributes: Mapping[str, Any], name: str = "") -> str:
    """Pick the AMP span type for a span from its attributes."""
    override = attributes.get(TYPE_OVERRIDE_KEY)
    if isinstance(override, str) and override in SPAN_TYPES:
        return override

    op = attributes.get("gen_ai.operation.name")
    if isinstance(op, str):
        op = op.lower()
        if op in _LLM_OPS:
            return "llm"
        if op in _TOOL_OPS:
            return "tool"
        if op in _AGENT_OPS:
            return "agent"
        if op in _RAG_OPS:
            return "rag"
        if op in _ORCH_OPS:
            return "orchestration"

    kind = attributes.get("openinference.span.kind")
    if isinstance(kind, str) and kind.upper() in _OPENINFERENCE_KIND:
        return _OPENINFERENCE_KIND[kind.upper()]

    keys = attributes.keys()
    if any(k.startswith("gen_ai.tool.") or k.startswith("tool.") for k in keys):
        return "tool"
    if any(k.startswith("rag.") or k.startswith("retrieval.") for k in keys) or "gen_ai.data_source.id" in keys:
        return "rag"
    if "gen_ai.agent.name" in keys or "gen_ai.agent.id" in keys:
        return "agent"
    if "gen_ai.request.model" in keys or "llm.model_name" in keys:
        return "llm"
    return "custom"


def apply_aliases(attributes: dict[str, Any]) -> list[str]:
    """Fill AMP's expected attribute names from equivalent ones. Returns targets added."""
    added = []
    for target, sources in ATTRIBUTE_ALIASES.items():
        if target in attributes:
            continue
        for src in sources:
            if src in attributes:
                attributes[target] = attributes[src]
                added.append(target)
                break
    return added


def ns_to_iso(ns: int | None) -> str | None:
    """Nanoseconds since epoch to ISO 8601 UTC with milliseconds, like JS toISOString()."""
    if ns is None:
        return None
    secs, rem = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{rem // 1_000_000:03d}Z"


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, Sequence):
        return [_json_value(v) for v in value]
    return str(value)


def _status(span: Any) -> tuple[str, str | None]:
    code = getattr(getattr(span, "status", None), "status_code", None)
    name = getattr(code, "name", str(code or "UNSET")).upper()
    desc = getattr(span.status, "description", None) if getattr(span, "status", None) else None
    if name == "ERROR":
        return "error", desc
    if name == "OK":
        return "ok", desc
    return "unset", desc


def span_to_amp(span: Any) -> dict[str, Any]:
    """Convert an OpenTelemetry ReadableSpan into an AMP Span dict."""
    ctx = span.context
    trace_id = format(ctx.trace_id, "032x")
    attrs = {k: _json_value(v) for k, v in (span.attributes or {}).items()}
    apply_aliases(attrs)
    status, status_message = _status(span)

    data: dict[str, Any] = {
        "span_id": format(ctx.span_id, "016x"),
        "trace_id": trace_id,
        "name": span.name,
        "type": classify(attrs, span.name),
        "start_time": ns_to_iso(span.start_time),
        "end_time": ns_to_iso(span.end_time),
        "status": status,
        "attributes": attrs,
    }
    parent = getattr(span, "parent", None)
    if parent is not None:
        data["parent_span_id"] = format(parent.span_id, "016x")
    if status_message:
        data["status_message"] = status_message
    if data["start_time"] is None:
        del data["start_time"]
    if data["end_time"] is None:
        del data["end_time"]

    events = []
    for ev in getattr(span, "events", None) or ():
        events.append({
            "name": ev.name,
            "timestamp": ns_to_iso(ev.timestamp),
            "attributes": {k: _json_value(v) for k, v in (ev.attributes or {}).items()},
        })
    if events:
        data["events"] = events

    resource = getattr(span, "resource", None)
    service = resource.attributes.get("service.name") if resource is not None else None
    if service:
        data["metadata"] = {"service.name": service}
    return data


def is_local_root(span: Any) -> bool:
    """True when this span starts the trace in this process (no parent, or a remote parent)."""
    parent = getattr(span, "parent", None)
    return parent is None or bool(getattr(parent, "is_remote", False))


def session_id_for(span: Any) -> str | None:
    attrs = span.attributes or {}
    for key in SESSION_KEYS:
        if attrs.get(key):
            return str(attrs[key])
    resource = getattr(span, "resource", None)
    if resource is not None:
        for key in SESSION_KEYS:
            if resource.attributes.get(key):
                return str(resource.attributes[key])
    return None
