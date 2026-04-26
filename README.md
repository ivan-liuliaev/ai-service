# Matchup Opinion Service (MVP v3)

Standalone FastAPI service for NBA pre-game matchup opinions.

## Scope

- NBA only
- `POST /analyze-matchup` with batch support
- Flexible optional stats context with stable request shape
- Deterministic spread engine based on `net_rating`, `line_hit_rate_l10`, `pace`, and `reb_differential`
- Real team names in the opinion text, not `Team1` placeholders
- Structured response fields for `verdict`, `projected_margin`, `cover_edge`, and `projected_score`
- LLM used for explanation sentences only; fallback copy is deterministic if Ollama fails

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open docs:

- `http://localhost:8000/docs`

## LLM setup (Ollama, optional)

If you want real local LLM output:

```bash
export OPINION_LLM_ENABLED=true
export OLLAMA_MODEL=qwen2.5:7b-instruct
export OLLAMA_URL=http://127.0.0.1:11434
export LLM_TIMEOUT_SECONDS=8
export LLM_MAX_RETRIES=1
export SPREAD_NO_BET_BAND=2.0
```

If LLM fails or is unavailable, the service still returns a deterministic three-sentence opinion.

## API example

### Request

```bash
curl -s -X POST http://localhost:8000/analyze-matchup \
  -H "Content-Type: application/json" \
  -d '{
    "matchups": [
      {
        "id":"1",
        "team1":"Boston Celtics",
        "team2":"Chicago Bulls",
        "context": {
          "team1": {
            "net_rating": 8.4,
            "line_hit_rate_l10": 70,
            "pace": 98.5,
            "reb_differential": 4.2
          },
          "team2": {
            "net_rating": -1.2,
            "line_hit_rate_l10": 30,
            "pace": 102.1,
            "reb_differential": -3.5
          },
          "shared": {
            "team1_spread": -4.5
          }
        }
      }
    ]
  }' | jq
```

### Response

```json
{
  "results": [
    {
      "id": "1",
      "verdict": "covers",
      "projected_margin": 11.0,
      "cover_edge": 6.5,
      "projected_score": {
        "team1": 117,
        "team2": 106
      },
      "opinion": "The Boston Celtics cover the -4.5 spread with a projected 117-106 score, a +11.0-point margin, and a +6.5-point edge. Boston Celtics' +8.4 net rating and 70% line hit rate put them well ahead of this number. A projected pace of 100.3 and a +4.2 rebounding differential reinforce their ability to stay ahead on the glass and the scoreboard."
    }
  ]
}
```

Spread convention used:

- `context.shared.team1_spread = -5.5` means Team1 is favored by 5.5
- `context.shared.team1_spread = +5.5` means Team1 is the underdog by 5.5

## Error behavior

- Invalid JSON, invalid shape, or empty fields -> `422`
- Duplicate `id` values in one request -> `400`
- Same-team matchup where `team1` and `team2` name the same team -> `400`
- Unexpected server error -> `500`

## Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Real LLM sanity suite (writes `reports/llm_sanity_latest.json`):

```bash
source .venv/bin/activate
export RUN_LLM_SANITY=1
export OPINION_LLM_ENABLED=true
export OLLAMA_MODEL=qwen2.5:7b-instruct
pytest -m llm_sanity -q
```

Sanity checks covered:

- valid request without context
- requested four-metric projection path
- commutative spread math on team swap
- duplicate id handling
- invalid payload handling
- LLM path, fallback path, and placeholder cleanup
- optional real-LLM regression run with saved report artifact

## Next

Keep the request shape stable and iterate on coefficients or prompt quality only when demo output needs it.
