from pathlib import Path
import sys

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))
from app.main import app

client = TestClient(app)


def test_analyze_matchup_success() -> None:
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
    assert body["results"][0]["opinion"]
    assert body["results"][1]["opinion"]


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
