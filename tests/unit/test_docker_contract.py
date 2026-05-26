"""Regression tests for the container trust/output contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_reviews_read_only_checkout_and_writes_reports_to_output_mount() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    review_service: dict[str, Any] = compose["services"]["review"]

    assert review_service["working_dir"] == "/app"
    assert review_service["environment"]["REPORT_DIR"] == "/reports"
    assert ".:/workspace:ro" in review_service["volumes"]
    assert "./reports:/reports" in review_service["volumes"]
    assert "./src:/app/src:ro" in review_service["volumes"]


def test_image_default_report_dir_is_separate_from_app_and_workspace() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "REPORT_DIR=/reports" in dockerfile
