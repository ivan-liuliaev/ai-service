# Matchup Opinion Service (MVP)

Standalone FastAPI service for your team backend to call over HTTP.

## What this MVP does

- Exposes `POST /analyze-matchup`
- Accepts batched matchups (`matchups[]`)
- Echoes each `id`
- Returns a stub freeform opinion string per matchup

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open docs at:

- `http://localhost:8000/docs`

## API example

### Request

```bash
curl -s -X POST http://localhost:8000/analyze-matchup \
  -H "Content-Type: application/json" \
  -d '{
    "matchups": [
      {"id":"1","team1":"Boston Celtics","team2":"New York Knicks"},
      {"id":"2","team1":"Lakers","team2":"Warriors"}
    ]
  }' | jq
```

### Response

```json
{
  "results": [
    {
      "id": "1",
      "opinion": "Stub opinion: slight lean toward Boston Celtics over New York Knicks based on generic form. MVP placeholder output, not betting advice."
    },
    {
      "id": "2",
      "opinion": "Stub opinion: slight lean toward Lakers over Warriors based on generic form. MVP placeholder output, not betting advice."
    }
  ]
}
```

## Error behavior

- Invalid JSON/shape/empty fields -> `422`
- Duplicate `id` values in one request -> `400`
- Unexpected server error -> `500`

## Tests (minimal smoke tests)

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Next iteration (LLM)

Keep this same endpoint/contract and swap the stub opinion generator with real LLM output.
