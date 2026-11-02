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

    # ---- AI providers ----
    # Any provider with credentials configured is enabled; its models show up in GET /models.
    OPENAI_API_KEY: str = ""
    OPENAI_CHAT_MODEL: str = "gpt-4o-mini"  # legacy name for the server default model
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_DIM: int = 1536
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_VERSION: str = "2024-10-21"
    # {"gpt-4o-mini": "my-deployment-name", ...} — which Azure deployment serves each model id.
    AZURE_OPENAI_DEPLOYMENTS: dict[str, str] = Field(default_factory=dict)
    OLLAMA_BASE_URL: str = ""
    # Comma separated model names served by Ollama (e.g. "llama3.1:8b,qwen2.5:14b").
    OLLAMA_MODELS: List[str] = Field(default_factory=list)
    # Server default; falls back to OPENAI_CHAT_MODEL, then the first available model.
    DEFAULT_CHAT_MODEL: str = ""
    # Optional JSON/YAML file with extra or overriding model specs (see app/ai/providers/catalog.py).
    MODEL_CATALOG_PATH: str = ""
    # Restrict the exposed models (comma separated ids). Empty = every model of an enabled provider.
    ENABLED_MODELS: List[str] = Field(default_factory=list)
    DISABLED_MODELS: List[str] = Field(default_factory=list)
    # {"gpt-4o": ["claude-sonnet-5-5"]}: tried in order when the primary provider fails.
    MODEL_FALLBACKS: dict[str, list[str]] = Field(default_factory=dict)
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
    # Hybrid retrieval: pgvector cosine + Postgres full-text, fused with reciprocal rank fusion.
    RAG_HYBRID_SEARCH: bool = True
    # How many candidates each retriever contributes before fusion/reranking (× top_k).
    RAG_CANDIDATES_MULTIPLIER: int = 4
    # none | llm — the llm reranker scores candidates with the chat model (extra call per turn).
    RAG_RERANKER: Literal["none", "llm"] = "none"
    # Rewrite follow-up questions into standalone search queries using the conversation.
    RAG_QUERY_REWRITE: bool = False
    # Prepend "document > section" headers to chunks before embedding (improves recall).
    RAG_CONTEXT_HEADERS: bool = True

    # ---- Agent ----
    AGENT_MAX_STEPS: int = 8
    AGENT_MAX_TOOL_SECONDS: int = 60
    # Tool names that need the user's approval before running (in addition to tools that
    # declare it themselves).
    TOOLS_REQUIRING_APPROVAL: List[str] = Field(default_factory=list)
    APPROVAL_TTL_MINUTES: int = 30

    # ---- Workspaces / quotas ----
    ORG_HEADER: str = "X-Org-Id"
    # Month-to-date usage is cached briefly so quota checks don't scan the usage table per turn.
    QUOTA_CACHE_SECONDS: int = 30

    # ---- Memory ----
    MEMORY_ENABLED: bool = True
    MEMORY_TOP_K: int = 6
    MEMORY_MAX_PER_USER: int = 500
    # New memories this similar to an existing one are treated as duplicates.
    MEMORY_DEDUP_SIMILARITY: float = 0.92

    # ---- Resumable streams / idempotency ----
    STREAM_EVENTS_TTL_SECONDS: int = 3600
    STREAM_EVENTS_MAXLEN: int = 5000
    IDEMPOTENCY_TTL_SECONDS: int = 86400

    # ---- Outbound web fetching (URL ingestion, fetch_url tool) ----
    WEB_FETCH_MAX_BYTES: int = 5 * 1024 * 1024
    WEB_FETCH_TIMEOUT_SECONDS: int = 20
    # Never enable in production: lets URL fetches reach private/loopback addresses (tests).
    WEB_FETCH_ALLOW_PRIVATE: bool = False
    HTTP_TRUST_ENV: bool = True

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

    @field_validator(
        "ALLOWED_ORIGINS",
        "ALLOWED_UPLOAD_EXTENSIONS",
        "OLLAMA_MODELS",
        "ENABLED_MODELS",
        "DISABLED_MODELS",
        mode="before",
    )
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

    @field_validator("MODEL_PRICING", "AZURE_OPENAI_DEPLOYMENTS", "MODEL_FALLBACKS", mode="before")
    @classmethod
    def parse_json_mapping(cls, v: Any):
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
