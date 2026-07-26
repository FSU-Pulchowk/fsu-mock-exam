"""
models.py — Pydantic request/response schemas.

Keeps route handlers clean and gives automatic OpenAPI docs.
"""

from pydantic import BaseModel, Field
from typing import Any


# ── Candidate Login ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    fullName: str = Field(..., min_length=2, max_length=128)
    email: str = Field(..., min_length=5, max_length=128)
    setSuffix: str = Field(..., min_length=1, max_length=16)


class LoginResponse(BaseModel):
    sessionToken: str
    studentName: str
    setSuffix: str


# ── /getQuestions response ────────────────────────────────────────────────────

class QuestionSetResponse(BaseModel):
    """Returned by GET /getQuestions when the exam is open."""
    id: str
    title: str
    total: int
    fullMarks: int
    durationSec: int
    negativePerWrong: float
    subjects: list[str]
    counts: dict[str, int]
    sections: dict[str, Any]
    questions: list[dict[str, Any]]
    isNotTime: bool = False


class NotTimeResponse(BaseModel):
    """Returned when the exam window has not started or has ended."""
    isNotTime: bool = True
    message: str


# ── /getAnswers response ──────────────────────────────────────────────────────

class AnswersResponse(BaseModel):
    questionSet: int
    answers: list[int]
    isNotTime: bool = False


# ── /submitAnswers ────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    studentId: str = Field(..., min_length=1, max_length=64)
    studentName: str | None = Field(default=None, max_length=128)
    studentEmail: str | None = Field(default=None, max_length=128)
    setSuffix: str | None = Field(default=None, max_length=32)
    answers: dict[str, int]   # {"1": 2, "3": 0, ...}  (question_no → option_index)


class SubmitResponse(BaseModel):
    received: bool = True
    studentId: str
    answered: int
    score: float
    maxScore: float


# ── /health ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    questions_loaded: bool
    answers_loaded: bool
    submissions_count: int
