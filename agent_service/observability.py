"""
ASES - Observability
Prometheus metrics + OpenTelemetry distributed tracing.

Metrics exposed at GET /metrics (scraped by Prometheus).
Traces exported via OTLP (Jaeger / Tempo / Grafana Cloud).

Usage:
    from observability import metrics, get_tracer, instrument_app

    # In FastAPI lifespan:
    instrument_app(app)

    # In agent_loop.py:
    with get_tracer().start_as_current_span("coder_agent") as span:
        span.set_attribute("model", config.coder_model)
        ...

    # Record a job completion:
    metrics.job_completed(tenant_id, task_type, success=True, duration_s=12.3, cost_usd=0.04)
"""

import os
import time
from contextlib import contextmanager
from typing import Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

try:
    from prometheus_client import (
        Counter, Histogram, Gauge, Info,
        CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
        multiprocess, REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    logger.warning("observability.prometheus_unavailable", hint="pip install prometheus-client")


def _make_metrics():
    if not _PROMETHEUS_AVAILABLE:
        return None

    class _Metrics:
        jobs_total = Counter(
            "ases_jobs_total",
            "Total jobs processed",
            ["tenant_id", "task_type", "status"],
        )
        job_duration = Histogram(
            "ases_job_duration_seconds",
            "Job processing time",
            ["task_type"],
            buckets=[1, 5, 15, 30, 60, 120, 300, 600],
        )
        job_cost = Histogram(
            "ases_job_cost_usd",
            "LLM cost per job in USD",
            ["tenant_id", "task_type"],
            buckets=[0.001, 0.01, 0.05, 0.10, 0.25, 0.50, 1.00, 2.00],
        )
        tokens_used = Counter(
            "ases_tokens_total",
            "Total LLM tokens consumed",
            ["tenant_id", "model"],
        )
        active_jobs = Gauge(
            "ases_active_jobs",
            "Jobs currently executing",
        )
        queue_depth = Gauge(
            "ases_queue_depth",
            "Jobs waiting in each priority queue",
            ["priority"],
        )
        sandbox_count = Gauge(
            "ases_active_sandboxes",
            "Docker sandbox containers currently running",
        )
        worker_count = Gauge(
            "ases_worker_count",
            "RQ worker processes",
        )
        llm_calls_total = Counter(
            "ases_llm_calls_total",
            "Total LLM API calls",
            ["model", "agent", "status"],
        )
        llm_latency = Histogram(
            "ases_llm_latency_seconds",
            "Time waiting for LLM response",
            ["model"],
            buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
        )
        billing_enforced = Counter(
            "ases_billing_enforced_total",
            "Jobs stopped by cost or token limits",
            ["tenant_id", "reason"],
        )

        # --- convenience wrappers ---

        def job_completed(
            self,
            tenant_id: str,
            task_type: str,
            success: bool,
            duration_s: float,
            cost_usd: float,
        ):
            status = "success" if success else "failure"
            self.jobs_total.labels(tenant_id=tenant_id, task_type=task_type, status=status).inc()
            self.job_duration.labels(task_type=task_type).observe(duration_s)
            self.job_cost.labels(tenant_id=tenant_id, task_type=task_type).observe(cost_usd)

        def llm_call_recorded(
            self,
            model: str,
            agent: str,
            duration_s: float,
            success: bool,
            tenant_id: str,
            tokens: int,
        ):
            status = "ok" if success else "error"
            self.llm_calls_total.labels(model=model, agent=agent, status=status).inc()
            self.llm_latency.labels(model=model).observe(duration_s)
            self.tokens_used.labels(tenant_id=tenant_id, model=model).inc(tokens)

        def update_infra_gauges(self):
            """Call periodically from a background task."""
            try:
                from scheduler import get_queue_depths, get_worker_count
                for priority, depth in get_queue_depths().items():
                    self.queue_depth.labels(priority=priority).set(depth)
                self.worker_count.set(get_worker_count())
            except Exception as e:
                logger.warning("observability.gauge_update_failed", error=str(e))

    return _Metrics()


metrics = _make_metrics()


def get_metrics_response():
    """Returns (body_bytes, content_type) for the /metrics endpoint."""
    if not _PROMETHEUS_AVAILABLE or metrics is None:
        return b"# prometheus-client not installed\n", "text/plain"
    return generate_latest(), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# OpenTelemetry tracing
# ---------------------------------------------------------------------------

_tracer = None


def _init_tracing():
    global _tracer
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name   = os.getenv("OTEL_SERVICE_NAME", "ases-agent-service")

    if not otlp_endpoint:
        logger.info("observability.tracing_disabled", hint="Set OTEL_EXPORTER_OTLP_ENDPOINT to enable")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _tracer = trace.get_tracer(service_name)

        # Auto-instrument HTTP and DB
        HTTPXClientInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()

        logger.info(
            "observability.tracing_enabled",
            endpoint=otlp_endpoint,
            service=service_name,
        )
    except ImportError as e:
        logger.warning(
            "observability.otel_unavailable",
            error=str(e),
            hint="pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc "
                 "opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx "
                 "opentelemetry-instrumentation-asyncpg",
        )


def get_tracer():
    """Returns the OTel tracer (or a no-op shim if tracing is disabled)."""
    if _tracer is not None:
        return _tracer
    return _NoOpTracer()


class _NoOpSpan:
    def set_attribute(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _NoOpTracer:
    def start_as_current_span(self, name, **kw):
        return _NoOpSpan()

    @contextmanager
    def start_span(self, name, **kw):
        yield _NoOpSpan()


def instrument_app(app) -> None:
    """
    Call once in FastAPI lifespan to attach Prometheus /metrics endpoint
    and initialise OTel tracing.
    """
    _init_tracing()

    # Auto-instrument FastAPI if OTel is available
    if _tracer is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
        except Exception as e:
            logger.warning("observability.fastapi_instrument_failed", error=str(e))

    from fastapi.responses import Response

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        if metrics:
            metrics.update_infra_gauges()
        body, content_type = get_metrics_response()
        return Response(content=body, media_type=content_type)

    logger.info("observability.instrumented")


# ---------------------------------------------------------------------------
# Context manager for timed LLM calls
# ---------------------------------------------------------------------------

@contextmanager
def trace_llm_call(model: str, agent: str, tenant_id: str = "unknown"):
    """
    Use around every call_model() invocation to capture latency + tokens.

    Example:
        with trace_llm_call("gpt-4o", "coder", tenant_id) as span:
            content, inp, out = await call_model(...)
            span["tokens"] = inp + out
    """
    ctx = {"tokens": 0, "success": True}
    t0 = time.perf_counter()
    try:
        yield ctx
    except Exception:
        ctx["success"] = False
        raise
    finally:
        duration = time.perf_counter() - t0
        if metrics:
            metrics.llm_call_recorded(
                model=model,
                agent=agent,
                duration_s=duration,
                success=ctx["success"],
                tenant_id=tenant_id,
                tokens=ctx["tokens"],
            )
