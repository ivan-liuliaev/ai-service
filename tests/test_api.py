from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from app.main import Matchup, MatchupContext, app, build_prompt

client = TestClient(app)


@pytest.fixture(autouse=True)
def default_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "false")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)


def test_analyze_matchup_success_without_context() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {"id": "1", "team1": "Boston Celtics", "team2": "New York Knicks"},
                {"id": "2", "team1": "Lakers", "team2": "Warriors"},
            ]
        },
    )
    assert response.status_code == 200

    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["id"] == "1"
    assert body["results"][1]["id"] == "2"
    assert body["results"][0]["opinion"].startswith("Lean: too close to call.")
    assert body["results"][1]["opinion"].startswith("Lean: too close to call.")


def test_analyze_matchup_accepts_partial_context_fields() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "1",
                    "team1": "Celtics",
                    "team2": "Knicks",
                    "context": {
                        "team1": {"pts": 118.4, "pts_allowed": 109.5, "pace": 99.2},
                        "team2": {"pts": 112.1, "pts_allowed": 115.0},
                        "shared": {"team1_spread": -4.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "1"
    assert body["results"][0]["opinion"].startswith("Lean: Team1 covers.")


def test_analyze_matchup_duplicate_id_returns_400() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {"id": "1", "team1": "A", "team2": "B"},
                {"id": "1", "team1": "C", "team2": "D"},
            ]
        },
    )
    assert response.status_code == 400
    assert "Duplicate matchup id" in response.json()["detail"]


def test_analyze_matchup_invalid_payload_returns_422() -> None:
    response = client.post("/analyze-matchup", json={"matchups": []})
    assert response.status_code == 422


def test_build_prompt_handles_flexible_metrics() -> None:
    matchup = Matchup(
        id="42",
        team1="A",
        team2="B",
        context=MatchupContext(
            team1={"custom_metric_x": 123, "injury_flag": False, "pts": 120, "pts_allowed": 108},
            team2={"custom_metric_y": 99.5, "pts": 110, "pts_allowed": 115},
            shared={"venue": "home-team1", "team1_spread": -5.5},
        ),
    )
    prompt = build_prompt(matchup)
    assert "custom_metric_x=123" in prompt
    assert "custom_metric_y=99.5" in prompt
    assert "venue=home-team1" in prompt
    assert "team1_spread: -5.5" in prompt
    assert "Fixed verdict: Lean: Team1 covers." in prompt


def test_llm_enabled_uses_generated_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(
        "app.main._call_ollama_with_retry",
        lambda _: (
            "Lean: Team1 covers. Better shooting and pace profile. "
            "Knicks can still close late if turnover margin swings."
        ),
    )

    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "1",
                    "team1": "Celtics",
                    "team2": "Knicks",
                    "context": {
                        "team1": {"pts": 120, "pts_allowed": 108, "last10_pts": 122, "pace": 100},
                        "team2": {"pts": 110, "pts_allowed": 116, "last10_pts": 109, "pace": 97},
                        "shared": {"team1_spread": -5.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    opinion = response.json()["results"][0]["opinion"]
    assert opinion.startswith("Lean: Team1 covers.")
    assert len(opinion.split()) <= 60


def test_llm_failure_falls_back_to_safe_opinion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")

    def fail_generation(_: str) -> str:
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr("app.main._call_ollama_with_retry", fail_generation)

    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "1",
                    "team1": "Celtics",
                    "team2": "Knicks",
                    "context": {
                        "team1": {"pts": 118},
                        "team2": {"pts": 112},
                        "shared": {"team1_spread": -2.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    opinion = response.json()["results"][0]["opinion"]
    assert opinion.startswith("Lean: too close to call.")
    assert "baseline projection" in opinion


def test_llm_text_without_verdict_gets_safe_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr("app.main._call_ollama_with_retry", lambda _: "Celtics likely win on pace.")

    response = client.post(
        "/analyze-matchup",
        json={"matchups": [{"id": "1", "team1": "Celtics", "team2": "Knicks"}]},
    )
    assert response.status_code == 200
    opinion = response.json()["results"][0]["opinion"]
    assert opinion.startswith("Lean: too close to call.")


def test_missing_spread_forces_too_close_even_if_llm_says_cover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(
        "app.main._call_ollama_with_retry",
        lambda _: "Lean: Team1 covers. Aggressive edge from offense.",
    )

    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "m1",
                    "team1": "A",
                    "team2": "B",
                    "context": {
                        "team1": {"pts": 120, "pts_allowed": 109},
                        "team2": {"pts": 112, "pts_allowed": 115},
                        "shared": {"rest_days_team1": 2},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["opinion"].startswith("Lean: too close to call.")


def test_small_cover_edge_forces_too_close() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "m2",
                    "team1": "A",
                    "team2": "B",
                    "context": {
                        "team1": {"pts": 115, "pts_allowed": 110, "last10_pts": 114, "pace": 98},
                        "team2": {"pts": 113, "pts_allowed": 111, "last10_pts": 113, "pace": 97.5},
                        "shared": {"team1_spread": -2.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["opinion"].startswith("Lean: too close to call.")


def test_large_positive_cover_edge_returns_cover() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "m3",
                    "team1": "A",
                    "team2": "B",
                    "context": {
                        "team1": {"pts": 120, "pts_allowed": 108, "last10_pts": 122, "pace": 100},
                        "team2": {"pts": 110, "pts_allowed": 116, "last10_pts": 109, "pace": 97},
                        "shared": {"team1_spread": -5.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["opinion"].startswith("Lean: Team1 covers.")
