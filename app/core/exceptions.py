from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logger import get_logger

logger = get_logger(__name__)


class AppException(Exception):
    """Base for all domain-level exceptions raised by the service.

    The HTTP layer maps these to a consistent JSON envelope so Flutter clients
    can always parse `{ "error": { "code", "message", "details" } }`.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppException):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnauthorizedError(AppException):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(AppException):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class ConflictError(AppException):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class RateLimitError(AppException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ValidationError(AppException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class AIProviderError(AppException):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ai_provider_error"


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(_: Request, exc: AppException):
        logger.warning("app_exception", code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "validation_error",
                "Request validation failed",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception):
        logger.exception("unhandled_exception", exc=str(exc))
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "Internal server error"),
        )
