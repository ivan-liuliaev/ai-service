from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from app.main import Matchup, MatchupContext, _determine_spread_decision, app, build_prompt

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
    assert body["results"][0]["verdict"] == "too_close_to_call"
    assert body["results"][0]["projected_margin"] is None
    assert body["results"][0]["cover_edge"] is None
    assert body["results"][0]["projected_score"] is None
    assert "Boston Celtics" in body["results"][0]["opinion"]
    assert "Team1" not in body["results"][0]["opinion"]


def test_analyze_matchup_uses_requested_metric_set() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "1",
                    "team1": "Boston Celtics",
                    "team2": "Chicago Bulls",
                    "context": {
                        "team1": {
                            "net_rating": 8.4,
                            "line_hit_rate_l10": 70,
                            "pace": 98.5,
                            "reb_differential": 4.2,
                        },
                        "team2": {
                            "net_rating": -1.2,
                            "line_hit_rate_l10": 30,
                            "pace": 102.1,
                            "reb_differential": -3.5,
                        },
                        "shared": {"team1_spread": -4.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "covers"
    assert result["projected_margin"] == pytest.approx(10.3, abs=0.1)
    assert result["cover_edge"] == pytest.approx(5.8, abs=0.1)
    assert result["projected_score"] == {"team1": 117, "team2": 106}
    assert result["opinion"].startswith("The Boston Celtics cover the -4.5 spread")
    assert "Team1" not in result["opinion"]


def test_projected_margin_is_commutative_on_team_swap() -> None:
    original = Matchup(
        id="a",
        team1="Boston Celtics",
        team2="Los Angeles Lakers",
        context=MatchupContext(
            team1={
                "net_rating": 8.4,
                "line_hit_rate_l10": 70,
                "pace": 98.5,
                "reb_differential": 4.2,
            },
            team2={
                "net_rating": -1.2,
                "line_hit_rate_l10": 30,
                "pace": 102.1,
                "reb_differential": -3.5,
            },
            shared={"team1_spread": -4.5},
        ),
    )
    swapped = Matchup(
        id="b",
        team1="Los Angeles Lakers",
        team2="Boston Celtics",
        context=MatchupContext(
            team1={
                "net_rating": -1.2,
                "line_hit_rate_l10": 30,
                "pace": 102.1,
                "reb_differential": -3.5,
            },
            team2={
                "net_rating": 8.4,
                "line_hit_rate_l10": 70,
                "pace": 98.5,
                "reb_differential": 4.2,
            },
            shared={"team1_spread": 4.5},
        ),
    )

    original_decision = _determine_spread_decision(original)
    swapped_decision = _determine_spread_decision(swapped)

    assert original_decision.projected_margin == pytest.approx(
        -swapped_decision.projected_margin,
        abs=1e-9,
    )
    assert original_decision.cover_edge == pytest.approx(
        -swapped_decision.cover_edge,
        abs=1e-9,
    )
    assert original_decision.verdict == "covers"
    assert swapped_decision.verdict == "does_not_cover"


def test_build_prompt_handles_flexible_metrics_and_team_names() -> None:
    matchup = Matchup(
        id="42",
        team1="Boston Celtics",
        team2="Chicago Bulls",
        context=MatchupContext(
            team1={
                "net_rating": 8.4,
                "line_hit_rate_l10": 70,
                "pace": 98.5,
                "reb_differential": 4.2,
                "custom_metric_x": 123,
                "injury_flag": False,
            },
            team2={
                "net_rating": -1.2,
                "line_hit_rate_l10": 30,
                "pace": 102.1,
                "reb_differential": -3.5,
                "custom_metric_y": 99.5,
            },
            shared={"venue": "home-team1", "team1_spread": -5.5},
        ),
    )

    prompt = build_prompt(matchup)
    assert "Boston Celtics" in prompt
    assert "Chicago Bulls" in prompt
    assert "custom_metric_x=123" in prompt
    assert "custom_metric_y=99.5" in prompt
    assert "team1_spread: -5.5" not in prompt
    assert "Spread: -5.5" in prompt
    assert "last 10 line hit rate=70%" in prompt


def test_llm_enabled_uses_generated_text_and_repairs_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(
        "app.main._call_ollama_with_retry",
        lambda _: (
            "Sentence 1: team1's dominant net rating and 70% line hit rate support the number. "
            "Sentence 2: team1's projected pace and rebounding edge keep this matchup under control."
        ),
    )

    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "1",
                    "team1": "Boston Celtics",
                    "team2": "Chicago Bulls",
                    "context": {
                        "team1": {
                            "net_rating": 8.4,
                            "line_hit_rate_l10": 70,
                            "pace": 98.5,
                            "reb_differential": 4.2,
                        },
                        "team2": {
                            "net_rating": -1.2,
                            "line_hit_rate_l10": 30,
                            "pace": 102.1,
                            "reb_differential": -3.5,
                        },
                        "shared": {"team1_spread": -4.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "covers"
    assert "Boston Celtics" in result["opinion"]
    assert "Chicago Bulls" not in result["opinion"] or "Bulls" in result["opinion"]
    assert "Team1" not in result["opinion"]
    assert "team1" not in result["opinion"].lower()


def test_llm_failure_falls_back_to_metric_based_copy(
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
                    "team1": "Los Angeles Lakers",
                    "team2": "Boston Celtics",
                    "context": {
                        "team1": {
                            "net_rating": -1.2,
                            "line_hit_rate_l10": 30,
                            "pace": 102.1,
                            "reb_differential": -3.5,
                        },
                        "team2": {
                            "net_rating": 8.4,
                            "line_hit_rate_l10": 70,
                            "pace": 98.5,
                            "reb_differential": 4.2,
                        },
                        "shared": {"team1_spread": -4.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "does_not_cover"
    assert "line hit rate" in result["opinion"]
    assert "rebounding differential" in result["opinion"]


def test_llm_numeric_hallucination_falls_back_to_deterministic_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPINION_LLM_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(
        "app.main._call_ollama_with_retry",
        lambda _: (
            "The Denver Nuggets' 3.5 net rating gap makes this line dangerous. "
            "Their 101.7 pace should still keep the game close."
        ),
    )

    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "hallucinated",
                    "team1": "Denver Nuggets",
                    "team2": "Portland Trail Blazers",
                    "context": {
                        "team1": {
                            "net_rating": 4.2,
                            "line_hit_rate_l10": 60,
                            "pace": 97.3,
                            "reb_differential": 1.5,
                        },
                        "team2": {
                            "net_rating": -0.5,
                            "line_hit_rate_l10": 50,
                            "pace": 98.8,
                            "reb_differential": -0.8,
                        },
                        "shared": {"team1_spread": -12.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "does_not_cover"
    assert "3.5" not in result["opinion"]
    assert "101.7" not in result["opinion"]
    assert "line hit rate" in result["opinion"]


def test_missing_spread_returns_projection_but_not_cover_edge() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "m1",
                    "team1": "New York Knicks",
                    "team2": "Cleveland Cavaliers",
                    "context": {
                        "team1": {
                            "net_rating": 2.1,
                            "line_hit_rate_l10": 80,
                            "pace": 95.2,
                            "reb_differential": 5.1,
                        },
                        "team2": {
                            "net_rating": 4.0,
                            "line_hit_rate_l10": 40,
                            "pace": 95.2,
                            "reb_differential": 0.0,
                        },
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "too_close_to_call"
    assert result["projected_margin"] is not None
    assert result["cover_edge"] is None
    assert result["projected_score"] is not None


def test_missing_net_rating_forces_too_close() -> None:
    response = client.post(
        "/analyze-matchup",
        json={
            "matchups": [
                {
                    "id": "m2",
                    "team1": "A",
                    "team2": "B",
                    "context": {
                        "team1": {"line_hit_rate_l10": 70, "pace": 99.0, "reb_differential": 3.0},
                        "team2": {"line_hit_rate_l10": 30, "pace": 97.0, "reb_differential": -2.0},
                        "shared": {"team1_spread": -2.5},
                    },
                }
            ]
        },
    )
    assert response.status_code == 200

    result = response.json()["results"][0]
    assert result["verdict"] == "too_close_to_call"
    assert result["projected_margin"] is None
    assert result["cover_edge"] is None
    assert result["projected_score"] is None


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
