"""One factory function, so the rest of the system never names a vendor."""

from __future__ import annotations

import os

from ..config import Settings
from .base import CallCache, LLMError, LLMProvider
from .fallback import FallbackProvider
from .providers import AnthropicProvider, GeminiProvider, OpenAICompatProvider
from .replay import ReplayProvider

#: Preset endpoints for OpenAI-compatible providers. ``json_mode`` records how
#: strictly each one can constrain output -- see OpenAICompatProvider.
_OPENAI_COMPATIBLE = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "json_mode": "object",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "json_mode": "object",
        "extra_headers": {"HTTP-Referer": "https://github.com/centralign/operator"},
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "json_mode": "schema",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "json_mode": "object",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "key_env": "TOGETHER_API_KEY",
        "json_mode": "object",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "key_env": "",
        "json_mode": "object",
    },
}

AVAILABLE = ("gemini", "anthropic", "replay", *sorted(_OPENAI_COMPATIBLE))


def build_provider(settings: Settings) -> LLMProvider:
    """Instantiate the configured provider.

    ``OPERATOR_LLM_MODEL`` may name several models, comma-separated, in which
    case they become a fallback chain -- see :mod:`centralign.llm.fallback`. The
    chain owns the cache so cassette keys stay stable regardless of which member
    served a given call.

    The cache directory doubles as the cassette store, which is why a live run
    automatically leaves behind everything needed to replay it offline.
    """
    name = settings.provider
    cache = CallCache(settings.cassette_dir, enabled=settings.llm_cache)

    if name == "replay":
        return ReplayProvider(cassette_dir=settings.cassette_dir)

    models = [m.strip() for m in settings.model.split(",") if m.strip()]
    if not models:
        raise LLMError("OPERATOR_LLM_MODEL is empty")

    if len(models) > 1:
        members = [_single(name, model, cache=None) for model in models]
        return FallbackProvider(members, name=name, cache=cache)
    return _single(name, models[0], cache=cache)


def _single(name: str, model: str, *, cache: CallCache | None) -> LLMProvider:
    """One concrete provider for one model."""
    if name == "gemini":
        return GeminiProvider(model, cache)
    if name == "anthropic":
        return AnthropicProvider(model, cache)

    preset = _OPENAI_COMPATIBLE.get(name)
    if preset is None:
        raise LLMError(
            f"unknown provider {name!r}. Available: {', '.join(AVAILABLE)}"
        )

    key_env = preset["key_env"]
    api_key = os.getenv(key_env, "") if key_env else ""
    if key_env and not api_key:
        raise LLMError(
            f"{key_env} is not set for provider {name!r}. Set it in .env, or run "
            "with OPERATOR_LLM_PROVIDER=replay to use recorded cassettes."
        )

    return OpenAICompatProvider(
        model,
        cache,
        name=name,
        base_url=str(preset["base_url"]),
        api_key=api_key,
        json_mode=str(preset["json_mode"]),
        extra_headers=preset.get("extra_headers"),  # type: ignore[arg-type]
    )
