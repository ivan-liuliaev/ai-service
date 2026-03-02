# API Contract (MVP v1)

## Endpoint

- `POST /analyze-matchup`

## Request JSON

```json
{
  "matchups": [
    {
      "id": "1",
      "team1": "Boston Celtics",
      "team2": "New York Knicks"
    }
  ]
}
```

### Request rules

- `matchups` is required and supports batching (1 to 50 items)
- Each matchup requires:
  - `id` (string, non-empty)
  - `team1` (string, non-empty)
  - `team2` (string, non-empty)
- Duplicate `id` values in the same request are rejected (`400`)

## Response JSON

```json
{
  "results": [
    {
      "id": "1",
      "opinion": "Stub opinion: slight lean toward Boston Celtics over New York Knicks based on generic form. MVP placeholder output, not betting advice."
    }
  ]
}
```

### Response rules

- `results` returns one object per input matchup
- `id` is echoed back from each input matchup
- `opinion` is freeform text (MVP placeholder for now)

## Status codes

- `200` success
- `400` contract/business rule error (e.g., duplicate ids)
- `422` malformed request payload
- `500` internal server error
