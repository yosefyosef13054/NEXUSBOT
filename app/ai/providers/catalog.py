"""Built-in model catalog.

Prices are USD per 1M tokens (input, output) at the time of writing; override or extend
with `MODEL_PRICING`, `MODEL_CATALOG_PATH` or the admin API without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

PROVIDERS = ("openai", "anthropic", "google", "azure", "ollama", "fake")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    display_name: str
    context_window: int = 128_000
    max_output_tokens: int = 4096
    input_usd_per_1m: float = 0.0
    output_usd_per_1m: float = 0.0
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    # Claude 4.6+ models reject the sampling parameters; the factory omits them.
    supports_temperature: bool = True
    tags: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def price_per_1k(self) -> tuple[float, float]:
        return self.input_usd_per_1m / 1000, self.output_usd_per_1m / 1000

    def to_public(self, *, is_default: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_streaming": self.supports_streaming,
            "input_usd_per_1m": self.input_usd_per_1m,
            "output_usd_per_1m": self.output_usd_per_1m,
            "tags": list(self.tags),
            "is_default": is_default,
        }


def _m(**kw: Any) -> ModelSpec:
    return ModelSpec(**kw)


BUILTIN_MODELS: tuple[ModelSpec, ...] = (
    # ---- OpenAI ----
    _m(
        id="gpt-4o-mini",
        provider="openai",
        display_name="GPT-4o mini",
        context_window=128_000,
        max_output_tokens=16_384,
        input_usd_per_1m=0.15,
        output_usd_per_1m=0.60,
        supports_vision=True,
        tags=("fast", "cheap"),
    ),
    _m(
        id="gpt-4o",
        provider="openai",
        display_name="GPT-4o",
        context_window=128_000,
        max_output_tokens=16_384,
        input_usd_per_1m=2.5,
        output_usd_per_1m=10.0,
        supports_vision=True,
        tags=("balanced",),
    ),
    _m(
        id="gpt-4.1",
        provider="openai",
        display_name="GPT-4.1",
        context_window=1_000_000,
        max_output_tokens=32_768,
        input_usd_per_1m=2.0,
        output_usd_per_1m=8.0,
        supports_vision=True,
        tags=("long-context",),
    ),
    _m(
        id="gpt-4.1-mini",
        provider="openai",
        display_name="GPT-4.1 mini",
        context_window=1_000_000,
        max_output_tokens=32_768,
        input_usd_per_1m=0.4,
        output_usd_per_1m=1.6,
        supports_vision=True,
        tags=("fast", "long-context"),
    ),
    _m(
        id="gpt-4.1-nano",
        provider="openai",
        display_name="GPT-4.1 nano",
        context_window=1_000_000,
        max_output_tokens=32_768,
        input_usd_per_1m=0.1,
        output_usd_per_1m=0.4,
        supports_vision=True,
        tags=("fastest", "cheapest"),
    ),
    # ---- Anthropic ----
    _m(
        id="claude-opus-5-5",
        provider="anthropic",
        display_name="Claude Opus 5.5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_usd_per_1m=4.0,
        output_usd_per_1m=20.0,
        supports_vision=True,
        supports_temperature=False,
        tags=("flagship", "reasoning"),
    ),
    _m(
        id="claude-sonnet-5-5",
        provider="anthropic",
        display_name="Claude Sonnet 5.5",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_usd_per_1m=2.0,
        output_usd_per_1m=10.0,
        supports_vision=True,
        supports_temperature=False,
        tags=("balanced", "coding"),
    ),
    _m(
        id="claude-haiku-5-5",
        provider="anthropic",
        display_name="Claude Haiku 5.5",
        context_window=1_000_000,
        max_output_tokens=64_000,
        input_usd_per_1m=0.1,
        output_usd_per_1m=0.5,
        supports_vision=True,
        supports_temperature=False,
        tags=("fast", "cheap"),
    ),
    _m(
        id="claude-fable-5-1",
        provider="anthropic",
        display_name="Claude Fable 5.1",
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_usd_per_1m=10.0,
        output_usd_per_1m=50.0,
        supports_vision=True,
        supports_temperature=False,
        tags=("most-capable",),
    ),
    # ---- Google ----
    _m(
        id="gemini-2.5-pro",
        provider="google",
        display_name="Gemini 2.5 Pro",
        context_window=1_000_000,
        max_output_tokens=65_536,
        input_usd_per_1m=1.25,
        output_usd_per_1m=10.0,
        supports_vision=True,
        tags=("reasoning", "long-context"),
    ),
    _m(
        id="gemini-2.5-flash",
        provider="google",
        display_name="Gemini 2.5 Flash",
        context_window=1_000_000,
        max_output_tokens=65_536,
        input_usd_per_1m=0.3,
        output_usd_per_1m=2.5,
        supports_vision=True,
        tags=("fast", "long-context"),
    ),
    _m(
        id="gemini-2.0-flash",
        provider="google",
        display_name="Gemini 2.0 Flash",
        context_window=1_000_000,
        max_output_tokens=8_192,
        input_usd_per_1m=0.1,
        output_usd_per_1m=0.4,
        supports_vision=True,
        tags=("fast", "cheap"),
    ),
    # ---- Fake (tests, keyless development) ----
    _m(
        id="nexusbot-fake",
        provider="fake",
        display_name="NexusBot fake model",
        context_window=32_000,
        max_output_tokens=4096,
        supports_vision=True,
        tags=("offline",),
    ),
)


def ollama_spec(name: str) -> ModelSpec:
    return ModelSpec(
        id=name,
        provider="ollama",
        display_name=f"{name} (local)",
        context_window=32_000,
        max_output_tokens=4096,
        tags=("local", "self-hosted"),
    )


def spec_from_dict(d: Mapping[str, Any]) -> ModelSpec:
    """Build a spec from a catalog file entry; unknown keys are ignored."""
    allowed = {f for f in ModelSpec.__dataclass_fields__}
    kw: dict[str, Any] = {k: v for k, v in d.items() if k in allowed}
    if "tags" in kw and isinstance(kw["tags"], list):
        kw["tags"] = tuple(kw["tags"])
    if "extra" in kw and kw["extra"] is None:
        kw["extra"] = {}
    return ModelSpec(**kw)
