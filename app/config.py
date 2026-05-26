from __future__ import annotations

from functools import lru_cache
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: List[str] = Field(default_factory=lambda: ["*"])

    # ---- Security ----
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
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

    # ---- Rate limiting ----
    RATE_LIMIT_PER_MINUTE: int = 60

    # ---- Storage ----
    UPLOAD_DIR: str = "/data/uploads"
    MAX_UPLOAD_MB: int = 25

    # ---- Observability ----
    SENTRY_DSN: str = ""
    LOG_LEVEL: str = "INFO"

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def split_origins(cls, v):
        if isinstance(v, str):
            if v.strip() == "*":
                return ["*"]
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
