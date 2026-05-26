from __future__ import annotations

import time
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration

from app.api.v1.router import api_router
from app.cache.redis_client import close_redis, get_redis
from app.config import settings
from app.core.exceptions import install_exception_handlers
from app.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        traces_sample_rate=0.1,
        integrations=[FastApiIntegration()],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app_starting", env=settings.APP_ENV)
    get_redis()  # eager connect
    yield
    await close_redis()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        debug=settings.APP_DEBUG,
        lifespan=lifespan,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url=f"{settings.API_V1_PREFIX}/docs",
        redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def request_timing(request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = str(int((time.time() - start) * 1000))
        return response

    install_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": settings.APP_NAME,
            "version": "1.0.0",
            "docs": f"{settings.API_V1_PREFIX}/docs",
        }

    return app


app = create_app()
