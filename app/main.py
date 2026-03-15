from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from numbers import Real
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

MetricValue = str | int | float | bool

app = FastAPI(
    title="Matchup Opinion Service",
    version="0.2.0",
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


class MatchupResult(BaseModel):
    id: str
    opinion: str


class AnalyzeMatchupResponse(BaseModel):
    results: list[MatchupResult]


@dataclass
class SpreadDecision:
    verdict: str
    spread: float | None
    projected_margin: float | None
    cover_edge: float | None
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


def _metric(metrics: dict[str, float], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    return float(value)


def _compute_projected_margin(matchup: Matchup) -> float | None:
    team1 = _as_numeric(matchup.context.team1 if matchup.context else None)
    team2 = _as_numeric(matchup.context.team2 if matchup.context else None)

    team1_pts = _metric(team1, "pts")
    team1_pts_allowed = _metric(team1, "pts_allowed")
    team2_pts = _metric(team2, "pts")
    team2_pts_allowed = _metric(team2, "pts_allowed")

    if None in (team1_pts, team1_pts_allowed, team2_pts, team2_pts_allowed):
        return None

    net1 = team1_pts - team1_pts_allowed
    net2 = team2_pts - team2_pts_allowed

    projected_margin = net1 - net2

    team1_last10 = _metric(team1, "last10_pts")
    team2_last10 = _metric(team2, "last10_pts")
    if team1_last10 is not None and team2_last10 is not None:
        projected_margin += 0.5 * (team1_last10 - team2_last10)

    team1_pace = _metric(team1, "pace")
    team2_pace = _metric(team2, "pace")
    if team1_pace is not None and team2_pace is not None:
        projected_margin += 0.2 * (team1_pace - team2_pace)

    return projected_margin


def _determine_spread_decision(matchup: Matchup) -> SpreadDecision:
    spread = _extract_team1_spread(matchup)
    if spread is None:
        return SpreadDecision(
            verdict="Lean: too close to call.",
            spread=None,
            projected_margin=None,
            cover_edge=None,
            reason="Spread input is missing, so cover evaluation is not reliable.",
        )

    projected_margin = _compute_projected_margin(matchup)
    if projected_margin is None:
        return SpreadDecision(
            verdict="Lean: too close to call.",
            spread=spread,
            projected_margin=None,
            cover_edge=None,
            reason="Core scoring/defense metrics are incomplete.",
        )

    cover_edge = projected_margin + spread
    no_bet_band = float(os.getenv("SPREAD_NO_BET_BAND", "2.0"))
    if abs(cover_edge) < no_bet_band:
        return SpreadDecision(
            verdict="Lean: too close to call.",
            spread=spread,
            projected_margin=projected_margin,
            cover_edge=cover_edge,
            reason=(
                f"Projected edge ({cover_edge:.1f}) is inside the no-bet band "
                f"(±{no_bet_band:.1f})."
            ),
        )

    verdict = (
        "Lean: Team1 covers."
        if cover_edge > 0
        else "Lean: Team1 does not cover."
    )
    return SpreadDecision(
        verdict=verdict,
        spread=spread,
        projected_margin=projected_margin,
        cover_edge=cover_edge,
        reason=(
            f"Projected margin is {projected_margin:.1f} vs required {-spread:.1f}, "
            f"edge {cover_edge:.1f}."
        ),
    )


def build_prompt(matchup: Matchup) -> str:
    decision = _determine_spread_decision(matchup)
    spread_line = (
        f"team1_spread: {decision.spread} (negative means team1 favored by that many points)"
        if decision.spread is not None
        else "team1_spread: missing"
    )
    return (
        "You are an NBA pre-game matchup assistant.\n"
        "The verdict is already computed by a deterministic model. Do not change it.\n"
        "Write exactly two short sentences:\n"
        "1) one or two reasons tied to provided metrics\n"
        "2) one uncertainty/risk note.\n"
        "Keep it under 50 words. No bullets. No verdict prefix. Not betting advice.\n\n"
        f"Fixed verdict: {decision.verdict}\n"
        f"Deterministic reason: {decision.reason}\n"
        f"Matchup: {matchup.team1} vs {matchup.team2}\n"
        f"{spread_line}\n"
        f"{_format_context_lines(matchup)}"
    )


def _normalize_opinion(text: str, max_words: int = 60) -> str:
    cleaned = " ".join(text.split())
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words]).rstrip(",;:") + "."
    return cleaned


def _strip_verdict_prefix(text: str) -> str:
    prefixes = (
        "Lean: Team1 covers.",
        "Lean: Team1 does not cover.",
        "Lean: too close to call.",
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            stripped = text[len(prefix) :].strip()
            if stripped:
                return stripped
            return "Deterministic edge is narrow, so uncertainty remains high."
    return text


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
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
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


def generate_opinion(matchup: Matchup) -> str:
    decision = _determine_spread_decision(matchup)
    if _llm_enabled():
        try:
            llm_text = _call_ollama_with_retry(build_prompt(matchup))
            explanation = _strip_verdict_prefix(_normalize_opinion(llm_text))
            return f"{decision.verdict} {explanation}"
        except Exception:  # noqa: BLE001
            pass
    return (
        f"{decision.verdict} {decision.reason} "
        "This is a baseline projection with uncertainty."
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

        seen_ids.add(matchup.id)
        results.append(
            MatchupResult(
                id=matchup.id,
                opinion=generate_opinion(matchup),
            )
        )

    return AnalyzeMatchupResponse(results=results)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, __) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
        },
    )
