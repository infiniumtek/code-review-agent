"""Shared literal types used across config and graph state."""

from __future__ import annotations

from typing import Literal

ProviderName = Literal["openai", "anthropic", "google"]
FailOnThreshold = Literal["off", "info", "low", "medium", "high", "critical"]
