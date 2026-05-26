"""LLM provider factory.

A single :func:`get_llm` switches over the three supported providers
(``openai`` · ``anthropic`` · ``google``) and returns a configured
``langchain`` chat model. Defaults come from :data:`config.settings`; resilience
knobs (``max_retries`` / ``timeout``) are applied uniformly.

``temperature`` is **silently omitted** for models whose API rejects it (the
OpenAI gpt-5 family and the o-series reasoning models, which accept only the
default), so ``DEFAULT_LLM_TEMPERATURE=0.0`` never breaks a gpt-5 run.

All three constructors accept ``model`` / ``api_key`` / ``timeout`` by name
(``populate_by_name=True``), so one uniform kwargs dict drives them.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from code_review_agent.config import Provider, Settings, get_settings

log = structlog.get_logger(__name__)

# Model-name prefixes whose API rejects an explicit ``temperature`` (only the
# default is accepted): OpenAI's gpt-5 family and the o-series reasoning models.
# Matched case-insensitively against the model name.
_NO_TEMPERATURE_PREFIXES: tuple[str, ...] = ("gpt-5", "o1", "o3", "o4")


class MissingAPIKeyError(RuntimeError):
    """Raised when the API key for the selected provider is not configured."""

    def __init__(self, provider: str, env_name: str) -> None:
        super().__init__(
            f"No API key for provider {provider!r}: set {env_name} "
            "(it must match DEFAULT_LLM_PROVIDER)."
        )
        self.provider = provider
        self.env_name = env_name


def _omits_temperature(model: str) -> bool:
    """True when ``model``'s API rejects an explicit ``temperature``."""
    return model.strip().lower().startswith(_NO_TEMPERATURE_PREFIXES)


def _require_key(key: str | None, env_name: str, provider: Provider) -> str:
    if not key:
        raise MissingAPIKeyError(provider, env_name)
    return key


def get_llm(
    provider: Provider | None = None,
    model: str | None = None,
    temperature: float | None = None,
    *,
    settings: Settings | None = None,
) -> BaseChatModel:
    """Build a chat model for ``provider``/``model`` (defaults from settings).

    ``temperature`` defaults to ``DEFAULT_LLM_TEMPERATURE`` and is dropped for
    models that reject it. Raises :class:`MissingAPIKeyError` if the selected
    provider's key is unset and :class:`ValueError` for an unknown provider.
    """
    settings = settings or get_settings()
    provider = provider or settings.default_llm_provider
    model = model or settings.default_llm_model
    if temperature is None:
        temperature = settings.default_llm_temperature

    kwargs: dict[str, Any] = {
        "model": model,
        "max_retries": settings.llm_max_retries,
        "timeout": settings.llm_timeout_seconds,
    }
    if _omits_temperature(model):
        log.debug("temperature_omitted", provider=provider, model=model)
    else:
        kwargs["temperature"] = temperature

    if provider == "openai":
        api_key = _require_key(settings.openai_api_key, "OPENAI_API_KEY", provider)
        return ChatOpenAI(api_key=api_key, **kwargs)
    if provider == "anthropic":
        api_key = _require_key(settings.anthropic_api_key, "ANTHROPIC_API_KEY", provider)
        return ChatAnthropic(api_key=api_key, **kwargs)
    if provider == "google":
        api_key = _require_key(settings.google_api_key, "GOOGLE_API_KEY", provider)
        return ChatGoogleGenerativeAI(api_key=api_key, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider!r} (expected openai|anthropic|google)")
