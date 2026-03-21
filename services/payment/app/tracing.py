import logging
import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)


def configure_tracing(app: FastAPI, engine):
    """Configure OpenTelemetry tracing for the payment service.

    Set JAEGER_ENABLED=true to export traces to Jaeger.
    When disabled, traces are still collected but not exported.
    """
    service_name = "payment-service"
    resource = Resource(attributes={SERVICE_NAME: service_name})
    tracer_provider = TracerProvider(resource=resource)

    jaeger_enabled = os.getenv("JAEGER_ENABLED", "true").lower() in (
        "true",
        "1",
        "yes",
    )

    if jaeger_enabled:
        try:
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            jaeger_exporter = JaegerExporter(
                agent_host_name=os.getenv("JAEGER_AGENT_HOST", "jaeger"),
                agent_port=int(os.getenv("JAEGER_AGENT_PORT", "6831")),
            )
            span_processor = BatchSpanProcessor(jaeger_exporter)
            tracer_provider.add_span_processor(span_processor)
            logger.info("Jaeger tracing enabled")
        except Exception as e:
            logger.warning(f"Jaeger tracing unavailable: {e}")
    else:
        logger.info("Jaeger tracing disabled (JAEGER_ENABLED=false)")

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
