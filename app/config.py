from __future__ import annotations

import json
import warnings
from functools import lru_cache
from typing import Any, List, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRETS = {"", "change-me", "changeme", "secret", "dev", "test"}


class Settings(BaseSettings):
    """Centralized application settings.

    Loaded once from environment variables / .env at process start.
    Inject everywhere via `Depends(get_settings)` — never import the global directly
    inside business logic to keep modules testable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---- App ----
    APP_NAME: str = "NexusBot"
    APP_ENV: Literal["development", "staging", "production", "test"] = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])

    # ---- Security ----
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # A refresh token presented again within this window (e.g. a mobile client retrying a
    # request that timed out) is treated as the same rotation, not as token theft.
    REFRESH_TOKEN_REUSE_GRACE_SECONDS: int = 15
    API_KEY_HEADER: str = "X-API-Key"

    # ---- Database ----
    DATABASE_URL: str
    SYNC_DATABASE_URL: str

    # ---- Redis / Queues ----
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # ---- AI ----
    OPENAI_API_KEY: str = ""
    OPENAI_CHAT_MODEL: str = "gpt-4o-mini"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_DIM: int = 1536
    # Run the whole stack without any provider key: deterministic fake LLM + embeddings.
    # Used by the test-suite and for local UI development.
    AI_FAKE_MODE: bool = False
    # Per-token delay of the fake model (lets tests exercise stop/disconnect mid-stream).
    AI_FAKE_DELAY_SECONDS: float = 0.0
    EMBEDDING_BATCH_SIZE: int = 64
    # Per-1K-token USD prices: {"model": [input, output]}. Merged over the built-in table.
    MODEL_PRICING: dict[str, list[float]] = Field(default_factory=dict)

    # ---- LangChain / tracing ----
    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "nexusbot"

    # ---- Vector store ----
    VECTOR_BACKEND: Literal["pgvector", "pinecone", "weaviate"] = "pgvector"
    PINECONE_API_KEY: str = ""
    PINECONE_INDEX: str = ""
    PINECONE_ENV: str = ""
    WEAVIATE_URL: str = ""
    WEAVIATE_API_KEY: str = ""

    # ---- RAG ----
    RAG_CHUNK_SIZE: int = 800
    RAG_CHUNK_OVERLAP: int = 120
    RAG_TOP_K: int = 5
    RAG_SIMILARITY_THRESHOLD: float = 0.20

    # ---- Chat / memory ----
    CHAT_MAX_HISTORY_MESSAGES: int = 20
    CHAT_SUMMARY_TRIGGER_TOKENS: int = 3000
    CHAT_RESPONSE_MAX_TOKENS: int = 1024
    CHAT_TEMPERATURE: float = 0.4
    # Hard cap on how long a single generation may run before it is cancelled.
    GENERATION_TIMEOUT_SECONDS: int = 180

    # ---- Rate limiting ----
    RATE_LIMIT_PER_MINUTE: int = 60
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10

    # ---- Storage ----
    UPLOAD_DIR: str = "/data/uploads"
    MAX_UPLOAD_MB: int = 25
    ALLOWED_UPLOAD_EXTENSIONS: List[str] = Field(
        default_factory=lambda: [
            ".pdf",
            ".docx",
            ".md",
            ".markdown",
            ".txt",
            ".html",
            ".htm",
            ".csv",
            ".json",
            ".log",
        ]
    )

    # ---- Background work ----
    # When true, post-turn maintenance (titles, summaries) runs inline instead of in a
    # background task. Only useful for deterministic tests.
    BACKGROUND_TASKS_INLINE: bool = False

    # ---- Observability ----
    SENTRY_DSN: str = ""
    LOG_LEVEL: str = "INFO"
    METRICS_ENABLED: bool = True

    @field_validator("ALLOWED_ORIGINS", "ALLOWED_UPLOAD_EXTENSIONS", mode="before")
    @classmethod
    def split_csv(cls, v: Any):
        if isinstance(v, str):
            s = v.strip()
            if s == "*":
                return ["*"]
            if s.startswith("["):
                return json.loads(s)
            return [o.strip() for o in s.split(",") if o.strip()]
        return v

    @field_validator("MODEL_PRICING", mode="before")
    @classmethod
    def parse_pricing(cls, v: Any):
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @model_validator(mode="after")
    def guard_production(self) -> Settings:
        insecure = self.JWT_SECRET_KEY.strip().lower() in _INSECURE_SECRETS or len(self.JWT_SECRET_KEY) < 32
        if self.is_production:
            if insecure:
                raise ValueError(
                    "JWT_SECRET_KEY must be a random string of at least 32 characters in production. "
                    "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
                )
            if self.APP_DEBUG:
                warnings.warn("APP_DEBUG=true in production; forcing it off.", stacklevel=2)
                object.__setattr__(self, "APP_DEBUG", False)
        elif insecure:
            warnings.warn(
                "JWT_SECRET_KEY is insecure; fine for development, refused in production.",
                stacklevel=2,
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_allow_credentials(self) -> bool:
        # Browsers reject `Access-Control-Allow-Origin: *` together with credentials.
        return self.ALLOWED_ORIGINS != ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
