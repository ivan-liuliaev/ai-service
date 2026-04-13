# API Contract (MVP v3)

## Endpoint

- `POST /analyze-matchup`

## Business context

- Purpose: produce a short NBA pre-game spread opinion per matchup.
- Non-goals: betting-grade certainty, live in-game prediction, financial advice.

## Request JSON

```json
{
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
}
```

### Request rules

- `matchups` is required and supports batching from 1 to 50 items.
- Each matchup requires:
  - `id` as a non-empty string
  - `team1` as a non-empty string
  - `team2` as a non-empty string
- `context` is optional.
- Within `context`:
  - `team1` is an optional metric map (`string -> string|number|boolean`)
  - `team2` is an optional metric map (`string -> string|number|boolean`)
  - `shared` is an optional metric map (`string -> string|number|boolean`)
- Recommended metrics for the current deterministic engine:
  - `team1.net_rating`, `team2.net_rating`
  - `team1.line_hit_rate_l10`, `team2.line_hit_rate_l10`
  - `team1.pace`, `team2.pace`
  - `team1.reb_differential`, `team2.reb_differential`
  - `shared.team1_spread`
- Any subset of fields is still accepted.
- Duplicate `id` values in the same request are rejected with `400`.

### Spread convention

- `team1_spread = -5.5`: Team1 is favored by 5.5.
- `team1_spread = +5.5`: Team1 is the underdog by 5.5.

## Response JSON

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

### Response rules

- `results` returns one object per input matchup.
- `id` echoes the input `id`.
- `verdict` is one of:
  - `covers`
  - `does_not_cover`
  - `too_close_to_call`
- `projected_margin` is the deterministic Team1 margin projection and may be `null` if the net-rating baseline is unavailable.
- `cover_edge` is `projected_margin + team1_spread` and may be `null` if the spread is missing or the margin cannot be computed.
- `projected_score` is an object with integer `team1` and `team2` projections, or `null` if the margin cannot be computed.
- `opinion` is freeform text, now expected to use actual team names and summarize the verdict, score or margin, edge, and the key metrics behind the call.

## Reliability behavior

- Verdict selection is deterministic from numeric projection plus a no-bet band.
- The math is commutative on team swap when the inputs and spread are inverted consistently.
- Ollama is used only for explanation sentences; the app owns the verdict sentence and numeric outputs.
- The service applies timeout and retry settings for Ollama.
- If Ollama fails, the service falls back to deterministic copy.

## Status codes

- `200` success
- `400` contract or business-rule error, such as duplicate ids
- `422` malformed request payload
- `500` internal server error
