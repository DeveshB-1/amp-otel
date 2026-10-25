"""Send OpenTelemetry traces from Python agents to Kore.ai AMP."""

from .assembler import TraceAssembler
from .client import AmpClient, SendResult
from .exporter import AmpSpanExporter
from .mapping import classify, span_to_amp

__all__ = ["AmpSpanExporter", "AmpClient", "SendResult", "TraceAssembler", "classify", "span_to_amp"]
__version__ = "0.1.0"
