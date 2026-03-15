# Matchup Opinion Service (MVP v2)

Standalone FastAPI service for NBA pre-game matchup opinions.

## Scope

- NBA only
- `POST /analyze-matchup` (batch support)
- Same response shape (`results[].id`, `results[].opinion`)
- Flexible optional stats context (add/remove fields without schema rewrites)
- Output target is Team1 spread-cover lean (`covers` / `does not cover` / `too close`)
- LLM-first when configured, safe fallback otherwise

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
```

If LLM fails/unavailable, service returns fallback opinion text.

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
        "team2":"New York Knicks",
        "context": {
          "team1": {"pts": 118.2, "pace": 99.1, "three_pt_made": 14.8},
          "team2": {"pts": 112.7},
          "shared": {"team1_spread": -5.5, "rest_days_team1": 2}
        }
      },
      {
        "id":"2",
        "team1":"Lakers",
        "team2":"Warriors"
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
      "opinion": "Lean: Team1 covers. The provided scoring and pace context supports Team1 relative to a -5.5 spread. Volatility remains high, so treat this as directional only."
    },
    {
      "id": "2",
      "opinion": "Lean: too close to call. Spread input is missing, so this uses a conservative baseline. Volatility remains high, so treat this as directional only."
    }
  ]
}
```

Spread convention used:

- `context.shared.team1_spread = -5.5` means Team1 is favored by 5.5 (must win by 6+ to cover)
- `context.shared.team1_spread = +5.5` means Team1 is underdog by 5.5 (can lose by up to 5 and still cover)

## Error behavior

- Invalid JSON/shape/empty fields -> `422`
- Duplicate `id` values in one request -> `400`
- Unexpected server error -> `500`

## Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Sanity checks covered:

- valid request without context
- valid request with partial context fields
- duplicate id handling
- invalid payload handling
- LLM path and fallback path

## Next

Keep contract stable and improve model/prompt quality incrementally.
