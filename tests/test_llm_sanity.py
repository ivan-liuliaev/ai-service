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
from app.main import Matchup, MatchupContext, _build_fallback_explanation, _determine_spread_decision, app

pytestmark = pytest.mark.llm_sanity

ALLOWED_VERDICTS = ("covers", "does_not_cover", "too_close_to_call")


def _model_available(model_name: str) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False

    models = payload.get("models", [])
    names = {entry.get("name", "") for entry in models}
    return model_name in names


def _load_cases() -> dict[str, object]:
    cases_path = Path(__file__).resolve().parent / "llm_sanity_cases.json"
    return json.loads(cases_path.read_text())


def _write_report(report: dict[str, object]) -> None:
    reports_dir = Path(__file__).resolve().parents[1] / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / "llm_sanity_latest.json"
    report_path.write_text(json.dumps(report, indent=2))


def _matchup_from_case(case: dict[str, object]) -> Matchup:
    context = case.get("context", {})
    return Matchup(
        id=str(case["id"]),
        team1=str(case["team1"]),
        team2=str(case["team2"]),
        context=MatchupContext(
            team1=context.get("team1"),
            team2=context.get("team2"),
            shared=context.get("shared"),
        ),
    )


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
    case_map = {row["id"]: row for row in payload["matchups"]}
    client = TestClient(app)
    response = client.post("/analyze-matchup", json=payload)
    assert response.status_code == 200

    body = response.json()
    assert len(body["results"]) == 5

    verdicts: dict[str, str] = {}
    report_rows: list[dict[str, object]] = []
    placeholder_hits = 0
    fallback_like_hits = 0
    decisive_case_count = 0

    for row in body["results"]:
        matchup_id = row["id"]
        opinion = row["opinion"]
        verdict = row["verdict"]
        case = case_map[matchup_id]
        matchup = _matchup_from_case(case)
        fallback = _build_fallback_explanation(matchup, _determine_spread_decision(matchup))

        assert verdict in ALLOWED_VERDICTS, f"{matchup_id}: invalid verdict"
        assert matchup.team1 in opinion, f"{matchup_id}: missing team1 name"
        assert "team1" not in opinion.lower(), f"{matchup_id}: placeholder leaked"
        assert row["projected_margin"] is not None, f"{matchup_id}: missing projected margin"
        assert row["projected_score"] is not None, f"{matchup_id}: missing projected score"

        if "team1" in opinion.lower() or "team2" in opinion.lower():
            placeholder_hits += 1
        if verdict in {"covers", "does_not_cover"}:
            decisive_case_count += 1
            if opinion.endswith(fallback):
                fallback_like_hits += 1

        verdicts[matchup_id] = verdict
        report_rows.append(
            {
                "id": matchup_id,
                "verdict": verdict,
                "projected_margin": row["projected_margin"],
                "cover_edge": row["cover_edge"],
                "opinion": opinion,
            }
        )

    assert placeholder_hits == 0, "LLM output should not leak team placeholders"
    assert fallback_like_hits < decisive_case_count, (
        "LLM sanity run should produce non-fallback copy for at least one decisive case"
    )

    directional_mismatches = 0
    if verdicts.get("case-5-missing-spread") != "too_close_to_call":
        directional_mismatches += 1
    if verdicts.get("case-2-favorite-too-big-line") not in {"too_close_to_call", "does_not_cover"}:
        directional_mismatches += 1
    if verdicts.get("case-3-underdog-can-cover") not in {"too_close_to_call", "covers"}:
        directional_mismatches += 1

    assert len(set(verdicts.values())) >= 2

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "summary": {
            "total_cases": len(report_rows),
            "placeholder_hits": placeholder_hits,
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
