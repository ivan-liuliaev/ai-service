from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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


def build_prompt(matchup: Matchup) -> str:
    spread = _extract_team1_spread(matchup)
    spread_line = (
        f"team1_spread: {spread} (negative means team1 favored by that many points)"
        if spread is not None
        else "team1_spread: missing"
    )
    return (
        "You are an NBA pre-game matchup assistant.\n"
        "Write exactly 2-3 short sentences with this structure:\n"
        "1) one of exactly: 'Lean: Team1 covers', 'Lean: Team1 does not cover', "
        "or 'Lean: too close to call'\n"
        "2) one or two metric-based reasons from provided context\n"
        "3) one uncertainty/risk note.\n"
        "Keep it under 60 words. No bullets. Not betting advice.\n\n"
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


def _enforce_spread_verdict(text: str) -> str:
    if text.startswith("Lean: Team1 covers"):
        return text
    if text.startswith("Lean: Team1 does not cover"):
        return text
    if text.startswith("Lean: too close to call"):
        return text
    return f"Lean: too close to call. {text}"


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


def build_fallback_opinion(matchup: Matchup) -> str:
    spread = _extract_team1_spread(matchup)
    team1_numeric = _as_numeric(matchup.context.team1 if matchup.context else None)
    team2_numeric = _as_numeric(matchup.context.team2 if matchup.context else None)

    comparable_keys = [key for key in team1_numeric if key in team2_numeric]
    team1_edges = sum(1 for key in comparable_keys if team1_numeric[key] > team2_numeric[key])
    team2_edges = sum(1 for key in comparable_keys if team2_numeric[key] > team1_numeric[key])
    edge_delta = team1_edges - team2_edges

    if spread is None:
        verdict = "Lean: too close to call."
        reason = "Spread input is missing, so this uses a conservative baseline"
        return f"{verdict} {reason}. Volatility remains high, so treat this as directional only."

    if comparable_keys:
        if spread < 0:
            if edge_delta >= 1:
                verdict = "Lean: Team1 covers."
            elif edge_delta <= -1:
                verdict = "Lean: Team1 does not cover."
            else:
                verdict = "Lean: too close to call."
        else:
            if edge_delta >= 1:
                verdict = "Lean: Team1 covers."
            elif edge_delta <= -2:
                verdict = "Lean: Team1 does not cover."
            else:
                verdict = "Lean: too close to call."
        reason = "Compared metrics versus the spread are mixed" if verdict.endswith("call.") else "Compared metrics support this spread lean"
    else:
        verdict = "Lean: too close to call."
        reason = "Context is limited, so this uses a conservative baseline"

    return f"{verdict} {reason}. Volatility remains high, so treat this as directional only."


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
    if _llm_enabled():
        try:
            llm_text = _call_ollama_with_retry(build_prompt(matchup))
            return _enforce_spread_verdict(_normalize_opinion(llm_text))
        except Exception:  # noqa: BLE001
            pass
    return build_fallback_opinion(matchup)


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
