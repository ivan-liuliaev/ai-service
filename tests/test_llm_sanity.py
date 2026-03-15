from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from app.main import app

pytestmark = pytest.mark.llm_sanity

ALLOWED_PREFIXES = (
    "Lean: Team1 covers",
    "Lean: Team1 does not cover",
    "Lean: too close to call",
)

UNCERTAINTY_MARKERS = ("risk", "uncertain", "variance", "volatility", "could", "can still")


def _model_available(model_name: str) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False

    models = payload.get("models", [])
    names = {entry.get("name", "") for entry in models}
    return model_name in names


def _extract_verdict(opinion: str) -> str:
    if opinion.startswith("Lean: Team1 covers"):
        return "covers"
    if opinion.startswith("Lean: Team1 does not cover"):
        return "not_cover"
    if opinion.startswith("Lean: too close to call"):
        return "too_close"
    return "unknown"


def _load_cases() -> dict[str, object]:
    cases_path = Path(__file__).resolve().parent / "llm_sanity_cases.json"
    return json.loads(cases_path.read_text())


def _write_report(report: dict[str, object]) -> None:
    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / "llm_sanity_latest.json"
    report_path.write_text(json.dumps(report, indent=2))


def test_llm_sanity_regression() -> None:
    if os.getenv("RUN_LLM_SANITY", "0").strip() != "1":
        pytest.skip("Set RUN_LLM_SANITY=1 to run real LLM sanity checks.")

    model_name = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    if not _model_available(model_name):
        pytest.skip(
            f"Ollama model '{model_name}' not available at http://127.0.0.1:11434."
        )

    os.environ["OPINION_LLM_ENABLED"] = "true"
    os.environ["OLLAMA_MODEL"] = model_name
    os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:11434")
    os.environ.setdefault("LLM_TIMEOUT_SECONDS", "30")
    os.environ.setdefault("LLM_MAX_RETRIES", "1")

    payload = _load_cases()
    client = TestClient(app)
    response = client.post("/analyze-matchup", json=payload)
    assert response.status_code == 200

    body = response.json()
    assert len(body["results"]) == 5

    verdicts: dict[str, str] = {}
    report_rows: list[dict[str, str]] = []
    uncertainty_hits = 0
    fallback_like_hits = 0

    for row in body["results"]:
        matchup_id = row["id"]
        opinion = row["opinion"]
        verdict = _extract_verdict(opinion)

        assert opinion.startswith(ALLOWED_PREFIXES), f"{matchup_id}: invalid prefix"
        if any(marker in opinion.lower() for marker in UNCERTAINTY_MARKERS):
            uncertainty_hits += 1
        if "baseline projection" in opinion.lower():
            fallback_like_hits += 1

        verdicts[matchup_id] = verdict
        report_rows.append({"id": matchup_id, "verdict": verdict, "opinion": opinion})

    assert uncertainty_hits >= 4, "Expected uncertainty marker in most outputs"
    assert fallback_like_hits == 0, "LLM sanity run should not rely on fallback output"

    directional_mismatches = 0
    if verdicts.get("case-5-missing-spread") != "too_close":
        directional_mismatches += 1
    if verdicts.get("case-2-favorite-too-big-line") not in {"too_close", "not_cover"}:
        directional_mismatches += 1
    if verdicts.get("case-3-underdog-can-cover") not in {"too_close", "covers"}:
        directional_mismatches += 1

    assert len(set(verdicts.values())) >= 2

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "summary": {
            "total_cases": len(report_rows),
            "uncertainty_hits": uncertainty_hits,
            "fallback_like_hits": fallback_like_hits,
            "directional_mismatches": directional_mismatches,
            "verdicts": verdicts,
        },
        "rows": report_rows,
    }
    _write_report(report)
    assert directional_mismatches <= 2, (
        f"Directional spread mismatches too high: {directional_mismatches} (max allowed: 2)"
    )
