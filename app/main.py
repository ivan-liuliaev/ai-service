from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from numbers import Real
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

MetricValue = str | int | float | bool

OLLAMA_SYSTEM_PROMPT = (
    "You are a sharp NBA spread analyst. The opening verdict sentence is already fixed "
    "by the application, so do not rewrite or contradict it. Write exactly two follow-up "
    "sentences that explain the case using the supplied variables. Explicitly use real team "
    "names, never placeholders like team1 or team2. Sentence one must tie net rating and "
    "line_hit_rate_l10 to this specific spread. Sentence two must explain how projected pace "
    "and rebounding differential shape the cover case. Use natural language metric names instead "
    "of snake_case field names, and use the supplied projected matchup pace when discussing tempo. "
    "Do not mention the opponent by name or cite opponent metrics in the follow-up sentences. "
    "If the verdict is too_close_to_call, explain that the line is too tight and do not say cover "
    "or fail to cover. Keep the tone authoritative. No bullets, no numbering, no JSON, no disclaimers."
)

app = FastAPI(
    title="Matchup Opinion Service",
    version="0.3.0",
    description="MVP AI-service for NBA pre-game matchup opinions.",
)


class MatchupContext(BaseModel):
    team1: dict[str, MetricValue] | None = None
    team2: dict[str, MetricValue] | None = None
    shared: dict[str, MetricValue] | None = None


class Matchup(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    team1: Annotated[str, Field(min_length=1, max_length=100)]
    team2: Annotated[str, Field(min_length=1, max_length=100)]
    context: MatchupContext | None = None


class AnalyzeMatchupRequest(BaseModel):
    matchups: Annotated[list[Matchup], Field(min_length=1, max_length=50)]


class ProjectedScore(BaseModel):
    team1: int
    team2: int


class MatchupResult(BaseModel):
    id: str
    verdict: str
    projected_margin: float | None
    cover_edge: float | None
    projected_score: ProjectedScore | None
    warnings: list[str]
    opinion: str


class AnalyzeMatchupResponse(BaseModel):
    results: list[MatchupResult]


@dataclass
class SpreadDecision:
    verdict: str
    spread: float | None
    projected_margin: float | None
    cover_edge: float | None
    projected_total: float | None
    projected_score_team1: int | None
    projected_score_team2: int | None
    projected_pace: float | None
    reason: str


def _format_metrics(metrics: dict[str, MetricValue] | None) -> str:
    if not metrics:
        return "none"
    parts = [f"{key}={value}" for key, value in metrics.items()]
    return ", ".join(parts)


def _format_context_lines(matchup: Matchup) -> str:
    if not matchup.context:
        return "team1_metrics: none\nteam2_metrics: none\nshared_metrics: none"
    return (
        f"team1_metrics: {_format_metrics(matchup.context.team1)}\n"
        f"team2_metrics: {_format_metrics(matchup.context.team2)}\n"
        f"shared_metrics: {_format_metrics(matchup.context.shared)}"
    )


def _normalize_team_name(team_name: str) -> str:
    return " ".join(team_name.casefold().split())


def _extract_team1_spread(matchup: Matchup) -> float | None:
    if not matchup.context or not matchup.context.shared:
        return None

    for key in ("team1_spread", "spread", "team1_line"):
        value = matchup.context.shared.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, Real):
            return float(value)
    return None


def _as_numeric(metrics: dict[str, MetricValue] | None) -> dict[str, float]:
    if not metrics:
        return {}
    numeric: dict[str, float] = {}
    for key, value in metrics.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, Real):
            numeric[key] = float(value)
    return numeric


def _team_metrics(matchup: Matchup) -> tuple[dict[str, float], dict[str, float]]:
    if not matchup.context:
        return {}, {}
    return _as_numeric(matchup.context.team1), _as_numeric(matchup.context.team2)


def _metric(metrics: dict[str, float], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    return float(value)


def _build_warnings(matchup: Matchup) -> list[str]:
    warnings: list[str] = []
    team1, team2 = _team_metrics(matchup)

    if _extract_team1_spread(matchup) is None:
        warnings.append("missing_team1_spread")
    if _metric(team1, "net_rating") is None or _metric(team2, "net_rating") is None:
        warnings.append("missing_net_rating")

    return warnings


def _normalize_hit_rate(value: float | None) -> float | None:
    if value is None:
        return None
    if abs(value) <= 1:
        return value * 100
    return value


def _format_signed(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def _format_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _format_pct(value: float | None) -> str:
    normalized = _normalize_hit_rate(value)
    if normalized is None:
        return "n/a"
    if normalized.is_integer():
        return f"{normalized:.0f}%"
    return f"{normalized:.1f}%"


def _format_spread(spread: float | None) -> str:
    if spread is None:
        return "missing"
    return f"{spread:+.1f}"


def _round_for_response(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 1)


def _display_projected_margin(decision: SpreadDecision) -> float | None:
    if (
        decision.projected_score_team1 is not None
        and decision.projected_score_team2 is not None
    ):
        return float(decision.projected_score_team1 - decision.projected_score_team2)
    return _round_for_response(decision.projected_margin)


def _display_cover_edge(decision: SpreadDecision) -> float | None:
    display_margin = _display_projected_margin(decision)
    if display_margin is None or decision.spread is None:
        return None
    return round(display_margin + decision.spread, 1)


def _compute_projected_margin(matchup: Matchup) -> float | None:
    team1, team2 = _team_metrics(matchup)

    team1_net_rating = _metric(team1, "net_rating")
    team2_net_rating = _metric(team2, "net_rating")
    if None in (team1_net_rating, team2_net_rating):
        return None

    projected_margin = 0.65 * (team1_net_rating - team2_net_rating)

    team1_line_hit_rate = _normalize_hit_rate(_metric(team1, "line_hit_rate_l10"))
    team2_line_hit_rate = _normalize_hit_rate(_metric(team2, "line_hit_rate_l10"))
    if team1_line_hit_rate is not None and team2_line_hit_rate is not None:
        projected_margin += 0.05 * (team1_line_hit_rate - team2_line_hit_rate)

    team1_reb_diff = _metric(team1, "reb_differential")
    team2_reb_diff = _metric(team2, "reb_differential")
    if team1_reb_diff is not None and team2_reb_diff is not None:
        projected_margin += 0.35 * (team1_reb_diff - team2_reb_diff)

    team1_pace = _metric(team1, "pace")
    team2_pace = _metric(team2, "pace")
    if team1_pace is not None and team2_pace is not None:
        projected_margin += 0.18 * (team1_pace - team2_pace)

    return projected_margin


def _compute_projected_pace(matchup: Matchup) -> float | None:
    team1, team2 = _team_metrics(matchup)
    team1_pace = _metric(team1, "pace")
    team2_pace = _metric(team2, "pace")

    if team1_pace is not None and team2_pace is not None:
        return (team1_pace + team2_pace) / 2
    if team1_pace is not None:
        return team1_pace
    if team2_pace is not None:
        return team2_pace
    return None


def _compute_projected_total(projected_pace: float | None) -> float:
    effective_pace = projected_pace if projected_pace is not None else 99.0
    return 220.0 + ((effective_pace - 99.0) * 2.2)


def _compute_projected_scores(
    projected_total: float | None,
    projected_margin: float | None,
) -> tuple[int | None, int | None]:
    if projected_total is None or projected_margin is None:
        return None, None

    total_points = int(round(projected_total))
    team1_score = int(round((projected_total + projected_margin) / 2))
    team2_score = total_points - team1_score
    return team1_score, team2_score


def _determine_spread_decision(matchup: Matchup) -> SpreadDecision:
    spread = _extract_team1_spread(matchup)
    projected_margin = _compute_projected_margin(matchup)
    projected_pace = _compute_projected_pace(matchup)
    projected_total = None
    projected_score_team1 = None
    projected_score_team2 = None

    if projected_margin is not None:
        projected_total = _compute_projected_total(projected_pace)
        projected_score_team1, projected_score_team2 = _compute_projected_scores(
            projected_total,
            projected_margin,
        )

    if spread is None:
        return SpreadDecision(
            verdict="too_close_to_call",
            spread=None,
            projected_margin=projected_margin,
            cover_edge=None,
            projected_total=projected_total,
            projected_score_team1=projected_score_team1,
            projected_score_team2=projected_score_team2,
            projected_pace=projected_pace,
            reason="Spread input is missing, so cover evaluation cannot be completed.",
        )

    if projected_margin is None:
        return SpreadDecision(
            verdict="too_close_to_call",
            spread=spread,
            projected_margin=None,
            cover_edge=None,
            projected_total=None,
            projected_score_team1=None,
            projected_score_team2=None,
            projected_pace=projected_pace,
            reason="The net_rating baseline is incomplete, so the model cannot price the spread.",
        )

    cover_edge = projected_margin + spread
    no_bet_band = float(os.getenv("SPREAD_NO_BET_BAND", "2.0"))
    if abs(cover_edge) < no_bet_band:
        return SpreadDecision(
            verdict="too_close_to_call",
            spread=spread,
            projected_margin=projected_margin,
            cover_edge=cover_edge,
            projected_total=projected_total,
            projected_score_team1=projected_score_team1,
            projected_score_team2=projected_score_team2,
            projected_pace=projected_pace,
            reason=(
                f"Projected edge ({cover_edge:.1f}) is inside the no-bet band "
                f"(±{no_bet_band:.1f})."
            ),
        )

    verdict = "covers" if cover_edge > 0 else "does_not_cover"
    return SpreadDecision(
        verdict=verdict,
        spread=spread,
        projected_margin=projected_margin,
        cover_edge=cover_edge,
        projected_total=projected_total,
        projected_score_team1=projected_score_team1,
        projected_score_team2=projected_score_team2,
        projected_pace=projected_pace,
        reason=(
            f"Projected margin is {projected_margin:.1f} against a {_format_spread(spread)} line, "
            f"for a {cover_edge:.1f}-point edge."
        ),
    )


def _build_opening_sentence(matchup: Matchup, decision: SpreadDecision) -> str:
    projected_score = (
        f"{decision.projected_score_team1}-{decision.projected_score_team2}"
        if decision.projected_score_team1 is not None and decision.projected_score_team2 is not None
        else "n/a"
    )
    projected_margin = _format_signed(_display_projected_margin(decision))
    cover_edge = _format_signed(_display_cover_edge(decision))

    if decision.verdict == "covers":
        return (
            f"The {matchup.team1} cover the {_format_spread(decision.spread)} spread with a projected "
            f"{projected_score} score, a {projected_margin}-point margin, and a {cover_edge}-point edge."
        )
    if decision.verdict == "does_not_cover":
        return (
            f"The {matchup.team1} do not cover the {_format_spread(decision.spread)} spread with a projected "
            f"{projected_score} score, a {projected_margin}-point margin, and a {cover_edge}-point edge."
        )
    if decision.spread is None:
        if decision.projected_margin is None:
            return (
                f"The {matchup.team1} vs {matchup.team2} spread is too close to call because no line was provided "
                "and the net rating baseline is incomplete."
            )
        return (
            f"The {matchup.team1} vs {matchup.team2} spread is too close to call because no line was provided, "
            f"though the model projects a {projected_score} score and a {projected_margin}-point margin."
        )
    if decision.projected_margin is None:
        return (
            f"The {matchup.team1} vs {matchup.team2} spread is too close to call at {_format_spread(decision.spread)} "
            "because the net rating baseline is incomplete."
        )
    return (
        f"The {matchup.team1} are too close to call on the {_format_spread(decision.spread)} spread with a projected "
        f"{projected_score} score, a {projected_margin}-point margin, and a {cover_edge}-point edge."
    )


def _build_metric_summary(matchup: Matchup) -> tuple[str, str]:
    team1, team2 = _team_metrics(matchup)
    team1_summary = (
        f"net rating={_format_signed(_metric(team1, 'net_rating'))}, "
        f"last 10 line hit rate={_format_pct(_metric(team1, 'line_hit_rate_l10'))}, "
        f"team pace={_format_number(_metric(team1, 'pace'))}, "
        f"rebounding differential={_format_signed(_metric(team1, 'reb_differential'))}"
    )
    team2_summary = (
        f"net rating={_format_signed(_metric(team2, 'net_rating'))}, "
        f"last 10 line hit rate={_format_pct(_metric(team2, 'line_hit_rate_l10'))}, "
        f"team pace={_format_number(_metric(team2, 'pace'))}, "
        f"rebounding differential={_format_signed(_metric(team2, 'reb_differential'))}"
    )
    return team1_summary, team2_summary


def build_prompt(matchup: Matchup, decision: SpreadDecision | None = None) -> str:
    decision = decision or _determine_spread_decision(matchup)
    team1_summary, team2_summary = _build_metric_summary(matchup)
    projected_score = (
        f"{decision.projected_score_team1}-{decision.projected_score_team2}"
        if decision.projected_score_team1 is not None and decision.projected_score_team2 is not None
        else "n/a"
    )
    return (
        f"Fixed opening sentence already written by the app: {_build_opening_sentence(matchup, decision)}\n"
        "Return exactly two follow-up sentences only.\n"
        f"Verdict key: {decision.verdict}\n"
        f"Team to evaluate: {matchup.team1}\n"
        f"Opponent: {matchup.team2}\n"
        f"Spread: {_format_spread(decision.spread)}\n"
        f"Projected score: {projected_score}\n"
        f"Projected margin: {_format_signed(_display_projected_margin(decision))}\n"
        f"Cover edge: {_format_signed(_display_cover_edge(decision))}\n"
        f"Projected matchup pace: {_format_number(decision.projected_pace)}\n"
        f"{matchup.team1} metrics: {team1_summary}\n"
        f"{matchup.team2} metrics: {team2_summary}\n"
        "All provided context:\n"
        f"{_format_context_lines(matchup)}"
    )


def _normalize_opinion(text: str, max_words: int = 90) -> str:
    cleaned = " ".join(text.split())
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words]).rstrip(",;:") + "."
    return cleaned


def _replace_placeholders(text: str, matchup: Matchup) -> str:
    replacements = {
        r"\bteam\s*1\b": matchup.team1,
        r"\bteam\s*2\b": matchup.team2,
    }
    cleaned = text
    for pattern, replacement in replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return cleaned


def _sanitize_explanation(text: str, matchup: Matchup) -> str:
    cleaned = text.strip().strip("\"'")
    cleaned = re.sub(r"(?i)sentence\s*[12]\s*:\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*\d+[\).\s-]+", "", cleaned)
    cleaned = _replace_placeholders(cleaned, matchup)
    return _normalize_opinion(cleaned)


def _sentence_count(text: str) -> int:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return len([part for part in parts if part])


def _normalize_numeric_token(token: str) -> str:
    raw = token.strip().rstrip(".,;:!?").replace("%", "").replace("+", "")
    raw = raw.lstrip("-")
    value = float(raw)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _allowed_numeric_tokens(matchup: Matchup, decision: SpreadDecision) -> set[str]:
    team1, team2 = _team_metrics(matchup)
    values: list[float] = [10.0]

    for metric_key in ("net_rating", "pace", "reb_differential"):
        for metrics in (team1, team2):
            value = _metric(metrics, metric_key)
            if value is not None:
                values.append(value)

    for metrics in (team1, team2):
        hit_rate = _normalize_hit_rate(_metric(metrics, "line_hit_rate_l10"))
        if hit_rate is not None:
            values.append(hit_rate)

    for value in (
        decision.spread,
        _display_projected_margin(decision),
        _display_cover_edge(decision),
        decision.projected_pace,
        decision.projected_score_team1,
        decision.projected_score_team2,
    ):
        if value is not None:
            values.append(float(value))

    return {_normalize_numeric_token(str(value)) for value in values}


def _llm_explanation_is_usable(
    explanation: str,
    matchup: Matchup,
    decision: SpreadDecision,
) -> bool:
    if _sentence_count(explanation) < 2:
        return False

    lowered = explanation.lower()
    if decision.verdict == "covers" and "does not cover" in lowered:
        return False
    if decision.verdict == "too_close_to_call" and "cover" in lowered:
        return False
    team2_tokens = [token for token in re.findall(r"[a-z]+", matchup.team2.lower()) if len(token) > 3]
    if decision.verdict == "does_not_cover" and (
        matchup.team2.lower() in lowered or any(token in lowered for token in team2_tokens)
    ):
        return False

    allowed_tokens = _allowed_numeric_tokens(matchup, decision)
    used_tokens = re.findall(r"[+-]?\d+(?:\.\d+)?%?", explanation)
    if any(_normalize_numeric_token(token) not in allowed_tokens for token in used_tokens):
        return False
    return True


def _build_fallback_explanation(matchup: Matchup, decision: SpreadDecision) -> str:
    team1, _ = _team_metrics(matchup)
    team1_net_rating = _metric(team1, "net_rating")
    team1_line_hit_rate = _metric(team1, "line_hit_rate_l10")
    team1_reb_diff = _metric(team1, "reb_differential")

    if decision.projected_margin is None:
        return (
            f"{matchup.team1}'s net rating baseline is missing, so the model cannot anchor a credible spread case. "
            "Without that baseline, the remaining indicators are not strong enough to force a side."
        )

    if decision.verdict == "covers":
        spread_take = "support this cover case"
        pace_take = "help them stay in control of the number"
    elif decision.verdict == "does_not_cover":
        spread_take = "push the model away from this price"
        pace_take = "leave them with very little margin for error"
    else:
        spread_take = "keep this number in toss-up territory"
        pace_take = "make it difficult to create clean separation"

    return (
        f"{matchup.team1}'s {_format_signed(team1_net_rating)} net rating and {_format_pct(team1_line_hit_rate)} "
        f"line hit rate over the last 10 games {spread_take}. "
        f"A projected pace of {_format_number(decision.projected_pace)} and a {_format_signed(team1_reb_diff)} "
        f"rebounding differential {pace_take}."
    )


def _llm_enabled() -> bool:
    raw = os.getenv("OPINION_LLM_ENABLED", "true").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    return bool(os.getenv("OLLAMA_MODEL"))


def _call_ollama(prompt: str) -> str:
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
    timeout_seconds = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))

    if not ollama_model:
        raise RuntimeError("OLLAMA_MODEL is required when LLM is enabled.")

    payload = {
        "model": ollama_model,
        "system": OLLAMA_SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    request = urllib.request.Request(
        url=f"{ollama_url}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as error:
        raise RuntimeError(f"Ollama call failed: {error}") from error

    generated = str(body.get("response", "")).strip()
    if not generated:
        raise RuntimeError("Ollama returned an empty response.")
    return generated


def _call_ollama_with_retry(prompt: str) -> str:
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "1"))
    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            return _call_ollama(prompt)
        except Exception as error:  # noqa: BLE001
            last_error = error
    raise RuntimeError("LLM generation failed after retries.") from last_error


def generate_opinion(matchup: Matchup, decision: SpreadDecision | None = None) -> str:
    decision = decision or _determine_spread_decision(matchup)
    opening_sentence = _build_opening_sentence(matchup, decision)
    if _llm_enabled() and decision.verdict in {"covers", "does_not_cover"}:
        try:
            llm_text = _call_ollama_with_retry(build_prompt(matchup, decision))
            explanation = _sanitize_explanation(llm_text, matchup)
            if explanation and _llm_explanation_is_usable(explanation, matchup, decision):
                return f"{opening_sentence} {explanation}"
        except Exception:  # noqa: BLE001
            pass
    return f"{opening_sentence} {_build_fallback_explanation(matchup, decision)}"


def _build_result(matchup: Matchup) -> MatchupResult:
    decision = _determine_spread_decision(matchup)
    projected_score = None
    if decision.projected_score_team1 is not None and decision.projected_score_team2 is not None:
        projected_score = ProjectedScore(
            team1=decision.projected_score_team1,
            team2=decision.projected_score_team2,
        )

    return MatchupResult(
        id=matchup.id,
        verdict=decision.verdict,
        projected_margin=_display_projected_margin(decision),
        cover_edge=_display_cover_edge(decision),
        projected_score=projected_score,
        warnings=_build_warnings(matchup),
        opinion=generate_opinion(matchup, decision),
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "matchup-opinion", "status": "running", "scope": "nba"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze-matchup", response_model=AnalyzeMatchupResponse)
def analyze_matchup(payload: AnalyzeMatchupRequest) -> AnalyzeMatchupResponse:
    seen_ids: set[str] = set()
    results: list[MatchupResult] = []

    for matchup in payload.matchups:
        if matchup.id in seen_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate matchup id '{matchup.id}' in request.",
            )
        if _normalize_team_name(matchup.team1) == _normalize_team_name(matchup.team2):
            raise HTTPException(
                status_code=400,
                detail=f"Matchup '{matchup.id}' must include two different teams.",
            )

        seen_ids.add(matchup.id)
        results.append(_build_result(matchup))

    return AnalyzeMatchupResponse(results=results)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, __) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
        },
    )
