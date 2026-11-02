"""Model registry: which models exist, which are usable, and how a request's model is resolved."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.ai.providers.catalog import BUILTIN_MODELS, ModelSpec, ollama_spec, spec_from_dict
from app.config import settings
from app.core.exceptions import ValidationError
from app.core.logger import get_logger

logger = get_logger(__name__)


def enabled_providers() -> set[str]:
    """Providers whose credentials/endpoints are configured. Fake mode hides the rest so a
    keyless environment never exposes models it cannot serve."""
    if settings.AI_FAKE_MODE:
        return {"fake"}
    out: set[str] = set()
    if settings.OPENAI_API_KEY:
        out.add("openai")
    if settings.ANTHROPIC_API_KEY:
        out.add("anthropic")
    if settings.GOOGLE_API_KEY:
        out.add("google")
    if settings.AZURE_OPENAI_API_KEY and settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_DEPLOYMENTS:
        out.add("azure")
    if settings.OLLAMA_BASE_URL and settings.OLLAMA_MODELS:
        out.add("ollama")
    return out


def _load_catalog_file(path: str) -> list[ModelSpec]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        logger.warning("model_catalog_missing", path=path)
        return []
    text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"MODEL_CATALOG_PATH is neither JSON nor YAML: {e}") from e
    if isinstance(data, dict):
        data = data.get("models", [])
    return [spec_from_dict(d) for d in data or []]


class ModelRegistry:
    def __init__(
        self,
        specs: Iterable[ModelSpec],
        *,
        providers: set[str],
        default_model: str | None,
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
    ) -> None:
        self._all: dict[str, ModelSpec] = {s.id: s for s in specs}
        allow_set = {a for a in allow if a}
        deny_set = {d for d in deny if d}
        self._enabled: dict[str, ModelSpec] = {
            s.id: s
            for s in self._all.values()
            if s.provider in providers and (not allow_set or s.id in allow_set) and s.id not in deny_set
        }
        self.default_model: str | None = None
        for candidate in (default_model, settings.OPENAI_CHAT_MODEL):
            if candidate and candidate in self._enabled:
                self.default_model = candidate
                break
        if self.default_model is None and self._enabled:
            self.default_model = next(iter(self._enabled))

    # ---- queries -------------------------------------------------------------

    def available(self) -> list[ModelSpec]:
        return list(self._enabled.values())

    def get(self, model_id: str | None) -> ModelSpec | None:
        if not model_id:
            return None
        return self._enabled.get(model_id)

    def known(self, model_id: str) -> bool:
        return model_id in self._all

    def spec(self, model_id: str | None) -> ModelSpec | None:
        """Any catalogued spec, enabled or not (pricing lookups must work for logged history)."""
        return self._all.get(model_id) if model_id else None

    def is_available(self, model_id: str | None) -> bool:
        return bool(model_id) and model_id in self._enabled

    @property
    def default(self) -> ModelSpec:
        if self.default_model is None:
            raise ValidationError(
                "No chat model is available: configure a provider key (OPENAI_API_KEY, "
                "ANTHROPIC_API_KEY, …) or set AI_FAKE_MODE=true"
            )
        return self._enabled[self.default_model]

    def validate(self, model_id: str | None) -> str | None:
        """Raise a 422 when an explicitly requested model cannot be served."""
        if model_id is None or model_id == "":
            return None
        if not self.is_available(model_id):
            raise ValidationError(
                f"Model '{model_id}' is not available",
                details={"model": model_id, "available": [s.id for s in self.available()]},
            )
        return model_id

    def resolve(self, *candidates: str | None) -> ModelSpec:
        """First usable candidate (request → session → user default), else the server default."""
        for c in candidates:
            if c and c in self._enabled:
                return self._enabled[c]
        return self.default

    def fallbacks_for(self, model_id: str) -> list[ModelSpec]:
        chain = settings.MODEL_FALLBACKS.get(model_id, []) if settings.MODEL_FALLBACKS else []
        return [self._enabled[m] for m in chain if m in self._enabled and m != model_id]


@lru_cache
def get_registry() -> ModelRegistry:
    specs: dict[str, ModelSpec] = {s.id: s for s in BUILTIN_MODELS}
    for name in settings.OLLAMA_MODELS:
        if name and name not in specs:
            specs[name] = ollama_spec(name)
    for model_id, deployment in (settings.AZURE_OPENAI_DEPLOYMENTS or {}).items():
        base = specs.get(model_id)
        extra = {"deployment": deployment}
        if base is not None:
            specs[f"azure/{model_id}"] = ModelSpec(
                **{
                    **base.__dict__,
                    "id": f"azure/{model_id}",
                    "provider": "azure",
                    "extra": extra,
                    "display_name": f"{base.display_name} (Azure)",
                }
            )
        else:
            specs[f"azure/{model_id}"] = ModelSpec(
                id=f"azure/{model_id}", provider="azure", display_name=f"{model_id} (Azure)", extra=extra
            )
    for spec in _load_catalog_file(settings.MODEL_CATALOG_PATH):
        specs[spec.id] = spec
    return ModelRegistry(
        specs.values(),
        providers=enabled_providers(),
        default_model=settings.DEFAULT_CHAT_MODEL or None,
        allow=settings.ENABLED_MODELS,
        deny=settings.DISABLED_MODELS,
    )


def reset_registry() -> None:
    """Tests change settings at runtime; clear the cache so the next call rebuilds."""
    get_registry.cache_clear()
