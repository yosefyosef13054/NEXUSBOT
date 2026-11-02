"""Prometheus metrics.

All metrics live in the default registry; `/metrics` renders them. Names are prefixed
with `nexusbot_` so they can coexist with other exporters on the same Prometheus.
"""

from __future__ import annotations

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "nexusbot_http_requests_total",
    "HTTP requests by method, route template and status code.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "nexusbot_http_request_duration_seconds",
    "Time to response start by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

LLM_REQUESTS = Counter(
    "nexusbot_llm_requests_total",
    "Model invocations by model, operation and outcome.",
    ["model", "operation", "status"],
)
LLM_TOKENS = Counter(
    "nexusbot_llm_tokens_total",
    "Tokens consumed by model and kind (prompt|completion|embedding).",
    ["model", "kind"],
)
LLM_LATENCY = Histogram(
    "nexusbot_llm_request_duration_seconds",
    "End-to-end generation latency by model and operation.",
    ["model", "operation"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)
LLM_COST_USD = Counter(
    "nexusbot_llm_cost_usd_total",
    "Estimated spend in USD by model.",
    ["model"],
)

TOOL_CALLS = Counter(
    "nexusbot_tool_calls_total",
    "Agent tool invocations by tool and outcome.",
    ["tool", "status"],
)
RAG_RETRIEVALS = Counter(
    "nexusbot_rag_retrievals_total",
    "Retrieval attempts by outcome (hit|empty|skipped).",
    ["outcome"],
)
INGEST_DOCUMENTS = Counter(
    "nexusbot_ingest_documents_total",
    "Document ingestions by final status.",
    ["status"],
)
ACTIVE_GENERATIONS = Gauge(
    "nexusbot_active_generations",
    "Generations currently streaming.",
)
APPROVALS = Counter(
    "nexusbot_approvals_total",
    "Agent approval requests by outcome (requested|approved|denied|expired).",
    ["outcome"],
)
INTERRUPTED_GENERATIONS = Counter(
    "nexusbot_interrupted_generations_total",
    "Generations stopped before completion (client disconnect or stop frame).",
    ["reason"],
)


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
