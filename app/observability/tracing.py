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
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        exporter = ConsoleSpanExporter()
        if settings.otel_exporter_otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            except Exception:  # noqa: BLE001 - 缺依赖时退回控制台，不影响主链路
                exporter = ConsoleSpanExporter()

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
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
