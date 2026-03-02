from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(
    title="Matchup Opinion Service",
    version="0.1.0",
    description="MVP AI-service skeleton for matchup opinions.",
)


class Matchup(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    team1: Annotated[str, Field(min_length=1, max_length=100)]
    team2: Annotated[str, Field(min_length=1, max_length=100)]


class AnalyzeMatchupRequest(BaseModel):
    matchups: Annotated[list[Matchup], Field(min_length=1, max_length=50)]


class MatchupResult(BaseModel):
    id: str
    opinion: str


class AnalyzeMatchupResponse(BaseModel):
    results: list[MatchupResult]


def build_stub_opinion(team1: str, team2: str) -> str:
    return (
        f"Stub opinion: slight lean toward {team1} over {team2} based on generic form. "
        "MVP placeholder output, not betting advice."
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "matchup-opinion", "status": "running"}


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
                opinion=build_stub_opinion(matchup.team1, matchup.team2),
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

