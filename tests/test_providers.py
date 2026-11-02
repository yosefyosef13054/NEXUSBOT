"""Model registry, resolution, provider factory and fallback configuration (no network)."""

from __future__ import annotations

import pytest

from app.ai.providers.catalog import BUILTIN_MODELS, ModelSpec, spec_from_dict
from app.ai.providers.factory import build_chat_model
from app.ai.providers.registry import enabled_providers, get_registry, reset_registry
from app.config import settings
from app.core.exceptions import ValidationError

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _spec(model_id: str) -> ModelSpec:
    return next(s for s in BUILTIN_MODELS if s.id == model_id)


def test_fake_mode_exposes_only_the_fake_model():
    registry = get_registry()
    assert [s.id for s in registry.available()] == ["nexusbot-fake"]
    assert registry.default.id == "nexusbot-fake"
    assert registry.validate(None) is None
    assert registry.validate("nexusbot-fake") == "nexusbot-fake"
    with pytest.raises(ValidationError) as exc:
        registry.validate("gpt-4o-mini")
    assert exc.value.details["available"] == ["nexusbot-fake"]
    # Unknown candidates fall through to the default instead of failing.
    assert registry.resolve("gpt-4o-mini", None, "bogus").id == "nexusbot-fake"


def test_providers_enabled_by_credentials(monkeypatch):
    monkeypatch.setattr(settings, "AI_FAKE_MODE", False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(settings, "MODEL_FALLBACKS", {"gpt-4o": ["claude-sonnet-5-5", "nope"]})
    reset_registry()
    assert enabled_providers() == {"openai", "anthropic"}
    registry = get_registry()
    ids = {s.id for s in registry.available()}
    assert {"gpt-4o-mini", "gpt-4o", "claude-opus-5-5", "claude-sonnet-5-5"} <= ids
    assert "gemini-2.5-pro" not in ids and "nexusbot-fake" not in ids
    assert registry.default.id == settings.OPENAI_CHAT_MODEL
    assert [s.id for s in registry.fallbacks_for("gpt-4o")] == ["claude-sonnet-5-5"]
    assert registry.resolve(None, "claude-opus-5-5").id == "claude-opus-5-5"
    reset_registry()


def test_allow_and_deny_lists(monkeypatch):
    monkeypatch.setattr(settings, "AI_FAKE_MODE", False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "ENABLED_MODELS", ["gpt-4o", "gpt-4o-mini"])
    monkeypatch.setattr(settings, "DISABLED_MODELS", ["gpt-4o-mini"])
    monkeypatch.setattr(settings, "DEFAULT_CHAT_MODEL", "gpt-4o")
    reset_registry()
    registry = get_registry()
    assert [s.id for s in registry.available()] == ["gpt-4o"]
    assert registry.default.id == "gpt-4o"
    reset_registry()


def test_azure_and_ollama_specs(monkeypatch):
    monkeypatch.setattr(settings, "AI_FAKE_MODE", False)
    monkeypatch.setattr(settings, "AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setattr(settings, "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setattr(settings, "AZURE_OPENAI_DEPLOYMENTS", {"gpt-4o-mini": "mini-deploy"})
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(settings, "OLLAMA_MODELS", ["llama3.1:8b"])
    reset_registry()
    registry = get_registry()
    ids = {s.id: s for s in registry.available()}
    assert ids["azure/gpt-4o-mini"].provider == "azure"
    assert ids["azure/gpt-4o-mini"].extra["deployment"] == "mini-deploy"
    assert ids["azure/gpt-4o-mini"].input_usd_per_1m == _spec("gpt-4o-mini").input_usd_per_1m
    assert ids["llama3.1:8b"].provider == "ollama" and "local" in ids["llama3.1:8b"].tags
    reset_registry()


def test_public_shape_and_catalog_entries():
    public = _spec("claude-opus-5-5").to_public(is_default=False)
    assert public["provider"] == "anthropic" and public["supports_tools"] and public["supports_vision"]
    assert public["input_usd_per_1m"] == 4.0 and public["output_usd_per_1m"] == 20.0
    spec = spec_from_dict(
        {"id": "my-model", "provider": "openai", "display_name": "Mine", "tags": ["x"], "bogus": 1}
    )
    assert spec.tags == ("x",) and spec.context_window == 128_000


def test_factory_omits_temperature_for_claude(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", "g-test")
    anthropic = build_chat_model(_spec("claude-opus-5-5"), temperature=0.7, max_tokens=256)
    assert anthropic.temperature is None  # Claude 4.6+ rejects sampling parameters
    assert anthropic.max_tokens == 256
    openai = build_chat_model(_spec("gpt-4o-mini"), temperature=0.7, max_tokens=256)
    assert openai.temperature == 0.7
    assert build_chat_model(_spec("nexusbot-fake")).__class__.__name__ == "FakeChatModel"
    google = build_chat_model(_spec("gemini-2.0-flash"), max_tokens=99_999)
    assert google.max_output_tokens == _spec("gemini-2.0-flash").max_output_tokens  # capped by the catalog


def test_estimate_cost_uses_catalog_prices():
    from app.utils.tokens import estimate_cost

    assert estimate_cost("claude-haiku-5-5", prompt_tokens=1_000_000) == pytest.approx(0.1)
    assert estimate_cost(
        "gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000
    ) == pytest.approx(0.75)
