"""FastAPI online answer service for Harborline Parcel."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import AuthError, verify_bearer_token
from app.retrieval import answer_question

app = FastAPI(title="Harborline Parcel Answer Service")


class AskRequest(BaseModel):
    question: str = Field(min_length=1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask")
def ask(
    payload: AskRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    try:
        user = verify_bearer_token(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail="Unauthorized") from exc

    result = answer_question(question=payload.question, roles=user.roles)
    return result.as_dict()
