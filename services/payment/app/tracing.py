import os
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from fastapi import FastAPI
from sqlalchemy import create_engine

# Configure OpenTelemetry
def configure_tracing(app: FastAPI, engine):
    # Set service name
    service_name = "payment-service"
    
    # Create resource with service name
    resource = Resource(attributes={
        SERVICE_NAME: service_name
    })
    
    # Create tracer provider
    tracer_provider = TracerProvider(resource=resource)
    
    # Create Jaeger exporter
    jaeger_exporter = JaegerExporter(
        agent_host_name=os.getenv("JAEGER_AGENT_HOST", "jaeger"),
        agent_port=int(os.getenv("JAEGER_AGENT_PORT", "6831")),
    )
    
    # Create batch span processor and add to tracer provider
    span_processor = BatchSpanProcessor(jaeger_exporter)
    tracer_provider.add_span_processor(span_processor)
    
    # Set tracer provider as global
    trace.set_tracer_provider(tracer_provider)
    
    # Instrument FastAPI
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    
    # Instrument SQLAlchemy
    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        tracer_provider=tracer_provider
    )
    
    return tracer_provider

# Get tracer
def get_tracer(name: str):
    return trace.get_tracer(name)