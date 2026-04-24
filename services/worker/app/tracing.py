import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)


# Configure OpenTelemetry
def configure_tracing(engine):
    # Set service name
    service_name = "auto-refund-worker"

    # Create resource with service name
    resource = Resource(attributes={SERVICE_NAME: service_name})

    # Create tracer provider
    tracer_provider = TracerProvider(resource=resource)

    # Only export to Jaeger if enabled
    jaeger_enabled = os.getenv("JAEGER_ENABLED", "true").lower() in ("true", "1", "yes")

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
            logger.info("Jaeger tracing enabled for worker")
        except Exception as e:
            logger.warning(f"Jaeger tracing unavailable for worker: {e}")
    else:
        logger.info("Jaeger tracing disabled for worker (JAEGER_ENABLED=false)")

    # Set tracer provider as global
    trace.set_tracer_provider(tracer_provider)

    # Instrument SQLAlchemy (graceful if not available)
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=tracer_provider)
    except Exception as e:
        logger.warning(f"SQLAlchemy instrumentation failed: {e}")

    return tracer_provider


# Get tracer
def get_tracer(name: str):
    return trace.get_tracer(name)
