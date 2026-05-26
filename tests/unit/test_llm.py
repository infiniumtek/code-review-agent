"""Unit tests for the LLM provider factory (mocked providers, no network)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from code_review_agent import llm
from code_review_agent.config import Provider, Settings
from code_review_agent.llm import MissingAPIKeyError, get_llm


class _FakeModel:
    """Records constructor kwargs in place of a real chat-model client."""

    provider = "?"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeOpenAI(_FakeModel):
    provider = "openai"


class _FakeAnthropic(_FakeModel):
    provider = "anthropic"


class _FakeGoogle(_FakeModel):
    provider = "google"


@pytest.fixture(autouse=True)
def _patch_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap real provider classes for kwargs-recording fakes."""
    monkeypatch.setattr(llm, "ChatOpenAI", _FakeOpenAI)
    monkeypatch.setattr(llm, "ChatAnthropic", _FakeAnthropic)
    monkeypatch.setattr(llm, "ChatGoogleGenerativeAI", _FakeGoogle)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "openai_api_key": "sk-openai",
        "anthropic_api_key": "sk-anthropic",
        "google_api_key": "sk-google",
        "llm_max_retries": 2,
        "llm_timeout_seconds": 60,
        "_env_file": None,  # isolate from any developer .env
    }
    base.update(overrides)
    return Settings(**base)


def test_openai_is_default_provider() -> None:
    model = cast(_FakeModel, get_llm(model="gpt-4o", settings=_settings()))
    assert model.provider == "openai"
    assert model.kwargs["api_key"] == "sk-openai"
    assert model.kwargs["model"] == "gpt-4o"
    assert model.kwargs["max_retries"] == 0
    assert model.kwargs["timeout"] == 60


def test_temperature_included_for_non_reasoning_model() -> None:
    model = cast(_FakeModel, get_llm(model="gpt-4o", temperature=0.0, settings=_settings()))
    assert model.kwargs["temperature"] == 0.0


@pytest.mark.parametrize("name", ["gpt-5-mini", "gpt-5", "GPT-5-Turbo", "o1", "o3-mini", "o4-mini"])
def test_temperature_omitted_for_gpt5_and_reasoning_models(name: str) -> None:
    model = cast(_FakeModel, get_llm(model=name, temperature=0.0, settings=_settings()))
    assert "temperature" not in model.kwargs


def test_anthropic_provider_routes_and_passes_temperature() -> None:
    model = cast(
        _FakeModel,
        get_llm(provider="anthropic", model="claude-sonnet-4-5", settings=_settings()),
    )
    assert model.provider == "anthropic"
    assert model.kwargs["api_key"] == "sk-anthropic"
    assert model.kwargs["temperature"] == 0.0


def test_google_provider_routes() -> None:
    model = cast(
        _FakeModel,
        get_llm(provider="google", model="gemini-2.5-pro", settings=_settings()),
    )
    assert model.provider == "google"
    assert model.kwargs["api_key"] == "sk-google"


def test_defaults_come_from_settings() -> None:
    settings = _settings(default_llm_provider="anthropic", default_llm_model="claude-x")
    model = cast(_FakeModel, get_llm(settings=settings))
    assert model.provider == "anthropic"
    assert model.kwargs["model"] == "claude-x"


def test_missing_key_for_selected_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    settings = _settings(openai_api_key=None)
    with pytest.raises(MissingAPIKeyError) as exc:
        get_llm(provider="openai", model="gpt-4o", settings=settings)
    assert exc.value.env_name == "OPENAI_API_KEY"


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        get_llm(provider=cast(Provider, "bogus"), model="x", settings=_settings())
