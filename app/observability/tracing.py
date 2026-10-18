from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.config import get_settings

_tracer = None


def setup_tracing() -> None:
    global _tracer
    settings = get_settings()
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("durableflow")
    except Exception:  # noqa: BLE001 - 观测不能影响主链路
        _tracer = None


@contextmanager
def span(name: str, attributes: dict | None = None) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in (attributes or {}).items():
            current.set_attribute(key, value)
        yield
