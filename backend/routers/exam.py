"""
routers/exam.py — Core exam API routes.

Endpoints
─────────
GET  /getQuestions   → returns full question set (checks exam time window)
GET  /getAnswers     → returns answer key (checks exam time window)
POST /submitAnswers  → stores student answers + computes score server-side
GET  /examInfo       → returns exam metadata (title, duration, marks) without questions

Design notes
────────────
• All data is served from the in-memory cache (cache.py) — zero disk I/O.
• Time-window checks use datetime.now(timezone.utc) to support any timezone.
• Score calculation is done server-side: negative marking applied per set config.
• Rate limit is applied per-endpoint via @limiter.limit() decorator.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import cache
import database
from config import EXAM_START, EXAM_END, RATE_LIMIT
from middleware.rate_limit import limiter
from models import (
    AnswersResponse,
    LoginRequest,
    LoginResponse,
    NotTimeResponse,
    SubmitRequest,
    SubmitResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Exam"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_exam_open() -> bool:
    """
    Returns True if the current UTC time is within the configured exam window.
    If neither EXAM_START nor EXAM_END is set, always returns True (open access).
    """
    if not EXAM_START and not EXAM_END:
        return True

    now = datetime.now(timezone.utc)

    if EXAM_START:
        start = datetime.fromisoformat(EXAM_START)
        # Make timezone-aware if naive
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if now < start:
            return False

    if EXAM_END:
        end = datetime.fromisoformat(EXAM_END)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if now > end:
            return False

    return True


def _calculate_score(
    student_answers: dict[str, int],
    correct_answers: list[int],
    negative_per_wrong: float,
    sections: dict,
    set_suffix: str | None = None,
) -> tuple[float, float]:
    """
    Server-side scoring with negative marking.

    Returns (score, max_score).

    correct_answers is a 0-indexed list; question numbers start at 1.
    Marks per question are determined by which section the question belongs to.
    """
    # Build a map: question_number (1-based) → marks_per_question
    marks_map: dict[int, float] = {}
    questions_data = cache.get_questions(set_suffix)
    if questions_data:
        for q in questions_data.get("questions", []):
            marks_map[q["no"]] = float(q.get("marks", 1))

    score = 0.0
    max_score = 0.0

    for i, correct_idx in enumerate(correct_answers):
        q_no = i + 1  # 1-based
        q_marks = marks_map.get(q_no, 1.0)
        max_score += q_marks

        student_idx = student_answers.get(str(q_no))

        if student_idx is None:
            # Unanswered — no penalty
            continue
        elif student_idx == correct_idx:
            score += q_marks
        else:
            # Wrong answer — apply negative marking
            score -= negative_per_wrong * q_marks

    # Round to 1 decimal place (matches original JS logic)
    score = round(score, 1)
    max_score = round(max_score, 1)
    return score, max_score


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Candidate Login",
    description="Registers an active exam session and issues a session token required to retrieve questions.",
)
@limiter.limit("20/minute")
async def login(request: Request, response: Response, payload: LoginRequest) -> LoginResponse:
    if not cache.is_questions_loaded(payload.setSuffix):
        raise HTTPException(status_code=404, detail="Selected question set is invalid or unavailable.")
    
    token = cache.create_session(payload.fullName, payload.email, payload.setSuffix)
    logger.info("[LOGIN] Session created for %s (%s) set=%s", payload.fullName, payload.email, payload.setSuffix)

    try:
        client_ip = request.client.host if request.client else None
        database.insert_session(
            name=payload.fullName,
            email=payload.email,
            set_suffix=payload.setSuffix,
            ip=client_ip,
        )
    except Exception as exc:
        logger.warning("[DB] Failed to persist session: %s", exc)

    return LoginResponse(
        sessionToken=token,
        studentName=payload.fullName,
        setSuffix=payload.setSuffix,
    )


@router.get(
    "/getQuestions",
    summary="Fetch exam questions",
    description=(
        "Returns the full question set if the exam window is open and a valid session token is provided. "
        "Returns `isNotTime: true` if the exam hasn't started or has ended."
    ),
)
@limiter.limit(RATE_LIMIT)
async def get_questions(
    request: Request,
    response: Response,
    set: str | None = None,
    authorization: str | None = Header(default=None),
):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1]

    session = cache.get_session(token) if token else None
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized. Active exam login session required.")

    set_to_load = session["set"] if session else (set or cache.get_default_suffix())
    questions = cache.get_questions(set_to_load)
    if questions is None:
        raise HTTPException(status_code=503, detail="Question data not loaded. Try again shortly.")

    if not _is_exam_open():
        logger.info("getQuestions blocked — exam window closed for %s", request.client.host)
        return JSONResponse(
            content=NotTimeResponse(
                isNotTime=True,
                message="The exam window has not started or has already ended."
            ).model_dump(),
            status_code=200,
        )

    logger.debug("getQuestions served to %s", request.client.host)
    return JSONResponse(content={**questions, "isNotTime": False})


@router.get(
    "/getAnswers",
    summary="Fetch answer key",
    description=(
        "Returns the answer key. "
        "Returns `isNotTime: true` if the exam hasn't started."
    ),
)
@limiter.limit(RATE_LIMIT)
async def get_answers(request: Request, response: Response):
    answers = cache.get_answers()
    if answers is None:
        raise HTTPException(status_code=503, detail="Answer data not loaded. Try again shortly.")

    if not _is_exam_open():
        return JSONResponse(
            content=NotTimeResponse(
                isNotTime=True,
                message="Answers are not available outside the exam window."
            ).model_dump(),
            status_code=200,
        )

    return JSONResponse(content={**answers, "isNotTime": False})


@router.post(
    "/submitAnswers",
    response_model=SubmitResponse,
    summary="Submit student answers",
    description=(
        "Accepts a studentId and answer map, computes score server-side "
        "with negative marking, and stores the result in memory."
    ),
)
@limiter.limit("10/minute")  # tighter limit — one real submission per student
async def submit_answers(request: Request, response: Response, payload: SubmitRequest) -> SubmitResponse:
    answers_data = cache.get_answers(payload.setSuffix)
    if answers_data is None:
        raise HTTPException(status_code=503, detail="Answer data not loaded.")

    questions_data = cache.get_questions(payload.setSuffix)
    if questions_data is None:
        raise HTTPException(status_code=503, detail="Question data not loaded.")

    correct = answers_data.get("answers", [])
    negative = float(questions_data.get("negativePerWrong", 0.1))
    sections = questions_data.get("sections", {})

    score, max_score = _calculate_score(
        payload.answers, correct, negative, sections, set_suffix=payload.setSuffix
    )

    cache.store_submission(
        student_id=payload.studentId,
        student_name=payload.studentName,
        student_email=payload.studentEmail,
        set_suffix=payload.setSuffix,
        answers=payload.answers,
        score=score,
    )

    try:
        database.insert_submission(
            student_id=payload.studentId,
            student_name=payload.studentName,
            student_email=payload.studentEmail,
            set_suffix=payload.setSuffix,
            score=score,
            answered=len(payload.answers),
        )
    except Exception as exc:
        logger.warning("[DB] Failed to persist submission: %s", exc)

    logger.info(
        "[SUBMIT] student=%s (%s / %s) set=%s answered=%d score=%.1f/%.1f",
        payload.studentId, payload.studentName or "Unknown", payload.studentEmail or "No Email", payload.setSuffix or "default", len(payload.answers), score, max_score,
    )

    return SubmitResponse(
        received=True,
        studentId=payload.studentId,
        answered=len(payload.answers),
        score=score,
        maxScore=max_score,
    )


@router.get(
    "/examInfo",
    summary="Exam metadata",
    description="Returns exam title, duration, marks structure -- without the questions. Useful for pre-load UI.",
)
@limiter.limit(RATE_LIMIT)
async def exam_info(request: Request, response: Response):
    questions = cache.get_questions()
    if questions is None:
        raise HTTPException(status_code=503, detail="Data not loaded.")

    # Strip the large questions array to keep this endpoint lightweight
    meta = {k: v for k, v in questions.items() if k != "questions"}
    meta["isNotTime"] = not _is_exam_open()
    return JSONResponse(content=meta)
