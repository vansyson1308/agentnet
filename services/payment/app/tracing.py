import logging
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)


def _otlp_traces_endpoint():
    """Where spans go. Standard OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT wins;
    otherwise the collector is ``http://$JAEGER_AGENT_HOST:$OTEL_EXPORTER_OTLP_PORT``
    (Jaeger all-in-one and every OTLP-capable backend listen on 4318/HTTP).
    Returns None when the exporter should resolve the endpoint itself."""
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    host = os.getenv("JAEGER_AGENT_HOST", "jaeger")
    port = os.getenv("OTEL_EXPORTER_OTLP_PORT", "4318")
    return f"http://{host}:{port}/v1/traces"


def _attach_otlp_exporter(tracer_provider, label: str) -> bool:
    """Export over OTLP/HTTP (provider-neutral). The legacy Jaeger thrift
    exporter is deprecated upstream and incompatible with current
    OpenTelemetry SDKs; JAEGER_AGENT_PORT (6831/UDP) is therefore ignored."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = _otlp_traces_endpoint()
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OTLP trace export enabled%s (%s)", f" for {label}" if label else "", endpoint or "endpoint from OTEL_EXPORTER_OTLP_*")
        return True
    except Exception as e:  # noqa: BLE001 — tracing must never take the service down
        logger.warning("OTLP trace export unavailable%s: %s", f" for {label}" if label else "", e)
        return False


def configure_tracing(app: FastAPI, engine):
    """Configure OpenTelemetry tracing for the payment service.

    Set JAEGER_ENABLED=true to export traces (OTLP/HTTP to Jaeger or any
    OTLP backend). When disabled, spans are still recorded in-process (and
    persisted to the ``spans`` table by the task path) but not exported.
    """
    service_name = "payment-service"
    resource = Resource(attributes={SERVICE_NAME: service_name})
    tracer_provider = TracerProvider(resource=resource)

    export_enabled = os.getenv("JAEGER_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )

    if export_enabled:
        _attach_otlp_exporter(tracer_provider, "")
    else:
        logger.info("Trace export disabled (JAEGER_ENABLED=false)")

    trace.set_tracer_provider(tracer_provider)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
        SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=tracer_provider)
    except Exception as e:
        logger.warning(f"OpenTelemetry instrumentation failed: {e}")

    return tracer_provider


def get_tracer(name: str):
    return trace.get_tracer(name)
