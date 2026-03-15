# API Contract (MVP v2)

## Endpoint

- `POST /analyze-matchup`

## Business context

- Purpose: produce short NBA pre-game spread-lean text per matchup.
- Non-goals: betting-grade certainty, live in-game prediction, financial advice.

## Request JSON

```json
{
  "matchups": [
    {
      "id": "1",
      "team1": "Boston Celtics",
      "team2": "New York Knicks",
      "context": {
        "team1": {
          "pts": 118.2,
          "pace": 99.1,
          "any_new_metric_name": 12
        },
        "team2": {
          "pts": 112.4
        },
        "shared": {
          "team1_spread": -5.5,
          "rest_days_team1": 2
        }
      }
    }
  ]
}
```

### Request rules

- `matchups` is required and supports batching (1 to 50 items).
- Each matchup requires:
  - `id` (string, non-empty)
  - `team1` (string, non-empty)
  - `team2` (string, non-empty)
- `context` is optional.
- Within `context`:
  - `team1` optional map of metrics (`string -> string|number|boolean`)
  - `team2` optional map of metrics (`string -> string|number|boolean`)
  - `shared` optional map of metrics (`string -> string|number|boolean`)
- Recommended spread field: `context.shared.team1_spread` (number).
- Any subset of fields is valid (partial context accepted).
- Duplicate `id` values in the same request are rejected (`400`).

### Spread convention

- `team1_spread = -5.5`: Team1 favored by 5.5 (Team1 must win by 6+ to cover).
- `team1_spread = +5.5`: Team1 underdog by 5.5 (Team1 covers if it loses by 5 or less, or wins).

## Response JSON

```json
{
  "results": [
    {
      "id": "1",
      "opinion": "Lean: Team1 covers. Provided metrics support Team1 versus the spread, but variance can still flip outcomes."
    }
  ]
}
```

### Response rules

- `results` returns one object per input matchup.
- `id` is echoed back from each input matchup.
- `opinion` is freeform text (2-3 short sentences target) and should start with one of:
  - `Lean: Team1 covers`
  - `Lean: Team1 does not cover`
  - `Lean: too close to call`

## Reliability behavior

- Tries local LLM when enabled/configured.
- Applies timeout and one retry by default.
- Falls back to deterministic opinion if LLM call fails.

## Status codes

- `200` success
- `400` contract/business rule error (e.g., duplicate ids)
- `422` malformed request payload
- `500` internal server error
