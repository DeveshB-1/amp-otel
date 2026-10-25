"""Validate an AMP telemetry payload against Kore.ai's published OpenAPI schema.

The schema file is copied from github.com/Koredotcom/amp-sdk at 45a7938
(openapi/amp-telemetry-sdk.yaml, MIT, Copyright (c) 2025 Kore.ai). One change:
upstream ends with a stray "d" line that makes the file invalid YAML, so it is
removed here (see upstream/amp-sdk-openapi-stray-char.patch). Needs the
`validate` extra (jsonschema, pyyaml).

    amp-otel-check payload.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

SPEC_PATH = Path(__file__).with_name("amp-telemetry-sdk.yaml")


@lru_cache(maxsize=1)
def _validator():
    import yaml
    from jsonschema import Draft202012Validator

    spec = yaml.safe_load(SPEC_PATH.read_text())
    schema = {"$ref": "#/components/schemas/TelemetryPayload", "components": spec["components"]}
    return Draft202012Validator(schema)


def _times(payload: dict[str, Any]) -> list[str]:
    errors = []
    for ti, trace in enumerate(payload.get("traces") or []):
        items = [(f"traces[{ti}]", trace)] + [(f"traces[{ti}].spans[{si}]", s) for si, s in enumerate(trace.get("spans") or [])]
        for where, obj in items:
            for key in ("start_time", "end_time"):
                if key in obj:
                    try:
                        datetime.fromisoformat(str(obj[key]).replace("Z", "+00:00"))
                    except ValueError:
                        errors.append(f"{where}.{key}: not ISO 8601: {obj[key]!r}")
            if where.endswith("]") and ".spans[" in where and obj.get("trace_id") != trace.get("trace_id"):
                errors.append(f"{where}.trace_id: does not match parent trace {trace.get('trace_id')!r}")
    return errors


def validate_payload(payload: Any) -> list[str]:
    """Return a list of human-readable problems; empty means the payload matches the spec."""
    errors = []
    for err in sorted(_validator().iter_errors(payload), key=lambda e: list(e.absolute_path)):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path).lstrip(".")
        errors.append(f"{path or '<root>'}: {err.message}")
    if isinstance(payload, dict):
        if not payload.get("traces") and not payload.get("transcripts"):
            errors.append("<root>: no traces or transcripts provided")
        errors.extend(_times(payload))
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: amp-otel-check payload.json [...]", file=sys.stderr)
        return 2
    bad = 0
    for path in argv:
        try:
            errors = validate_payload(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as e:
            errors = [f"cannot read JSON: {e}"]
        if errors:
            bad += 1
            print(f"FAIL {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {path}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
