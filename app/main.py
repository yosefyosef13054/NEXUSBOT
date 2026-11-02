from __future__ import annotations

from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration

from app.api.v1.router import api_router
from app.cache.redis_client import close_redis, get_redis
from app.config import settings
from app.core import background
from app.core.exceptions import install_exception_handlers
from app.core.logger import configure_logging, get_logger
from app.core.metrics import metrics_response
from app.core.middleware import RequestContextMiddleware, SelectiveGZipMiddleware

APP_VERSION = "1.1.0"

configure_logging()
logger = get_logger(__name__)

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        release=f"nexusbot@{APP_VERSION}",
        traces_sample_rate=0.1,
        integrations=[FastApiIntegration()],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app_starting", env=settings.APP_ENV, version=APP_VERSION, fake_ai=settings.AI_FAKE_MODE)
    get_redis()  # eager connect
    yield
    await background.drain()
    await close_redis()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=APP_VERSION,
        debug=settings.APP_DEBUG,
        lifespan=lifespan,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url=f"{settings.API_V1_PREFIX}/docs",
        redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    )

    # Middleware order: the last one added is the outermost.
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Process-Time-Ms", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware)

    install_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    if settings.METRICS_ENABLED:

        @app.get("/metrics", include_in_schema=False)
        async def metrics():
            return metrics_response()

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": settings.APP_NAME,
            "version": APP_VERSION,
            "docs": f"{settings.API_V1_PREFIX}/docs",
        }

    return app


app = create_app()
