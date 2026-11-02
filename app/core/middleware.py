from __future__ import annotations

import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

REQUEST_ID_HEADER = "X-Request-Id"
PROCESS_TIME_HEADER = "X-Process-Time-Ms"


def route_template(scope: Scope) -> str:
    """A low-cardinality label for the matched route (`/api/v1/sessions/{session_id}`)."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        prefix = scope.get("root_path", "")
        return f"{prefix}{path}" if prefix and not path.startswith(prefix) else path
    return "unmatched"


class RequestContextMiddleware:
    """Pure-ASGI middleware that:

    - assigns / propagates `X-Request-Id` and binds it to the structlog context,
    - stamps `X-Process-Time-Ms` (time to response start) on every response,
    - records Prometheus request counters and latency by route template.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER.lower())
        request_id = (incoming or "").strip()[:64] or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                headers[PROCESS_TIME_HEADER] = str(int((time.perf_counter() - start) * 1000))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            method = scope.get("method", "GET")
            route = route_template(scope)
            HTTP_REQUESTS.labels(method, route, str(status_code)).inc()
            HTTP_LATENCY.labels(method, route).observe(time.perf_counter() - start)
            structlog.contextvars.unbind_contextvars("request_id")


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZip everything except event streams.

    Starlette's GZipMiddleware buffers streaming bodies inside the zlib compressor, which
    delays Server-Sent Events frames indefinitely for clients that accept gzip (every Dart
    HttpClient does by default). SSE responses are tiny and latency-sensitive, so skip them.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "text/event-stream" in headers.get("accept", "") or scope["path"].endswith("/stream"):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
