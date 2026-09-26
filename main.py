"""Guardrail layer wrapping the Internal Operations RAG API."""
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RAG_API_URL = os.getenv("RAG_API_URL", "http://127.0.0.1:8000/ask")

app = FastAPI(title="Operations RAG Guardrail")


class AskRequest(BaseModel):
    question: str


# --- 1. Input guardrail: prompt injection detection ---
# This is a simple keyword-based first layer, not a complete defense.
# It catches common, obvious injection phrasing but can be bypassed by
# rephrasing or typos. Real systems layer this with classifier models.
INJECTION_PATTERNS = [
    r"ignore (all|previous|prior) instructions",
    r"disregard (all|previous|prior) instructions",
    r"reveal (your|the) system prompt",
    r"you are now",
    r"act as (a|an)",
    r"forget (everything|all) (you|that)",
    r"new instructions:",
]


def detect_prompt_injection(question: str) -> bool:
    """Return True if the question matches a known injection pattern."""
    lowered = question.lower()
    return any(re.search(pattern, lowered) for pattern in INJECTION_PATTERNS)


# --- 2. Output guardrail: groundedness check ---
# Simplified approach: checks whether key words from the answer also
# appear in the retrieved context. This is NOT true fact-verification -
# it can miss real hallucinations (fluent but unsupported claims) and
# can also flag correct answers that merely paraphrase the context.
def check_groundedness(answer: str, retrieved_context: list[str]) -> bool:
    """Return True if enough of the answer's key words appear in the context."""
    context_text = " ".join(retrieved_context).lower()
    # Pull out words that look meaningful (longer than 3 letters, alphanumeric)
    answer_words = set(re.findall(r"[a-zA-Z0-9]{4,}", answer.lower()))
    if not answer_words:
        return True  # nothing substantive to check
    matched = [word for word in answer_words if word in context_text]
    overlap_ratio = len(matched) / len(answer_words)
    return overlap_ratio >= 0.5  # at least half the key words should appear in context


# --- 3. Output guardrail: PII filtering ---
PII_PATTERNS = {
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    "phone": r"\b\d{10}\b|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b",
}


def mask_pii(text: str) -> tuple[str, bool]:
    """Replace detected PII patterns with [REDACTED]. Returns (masked_text, was_masked)."""
    masked = text
    found_any = False
    for label, pattern in PII_PATTERNS.items():
        if re.search(pattern, masked):
            found_any = True
            masked = re.sub(pattern, "[REDACTED]", masked)
    return masked, found_any


@app.post("/guarded-ask")
def guarded_ask(request: AskRequest) -> dict:
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    # --- Input guardrail check ---
    injection_detected = detect_prompt_injection(request.question)
    if injection_detected:
        return {
            "answer": "This question was blocked because it matched a known prompt injection pattern.",
            "flags": {"injection_detected": True, "grounded": None, "pii_masked": False},
        }

    # --- Call the RAG pipeline (Project 1) ---
    try:
        response = requests.post(RAG_API_URL, json={"question": request.question}, timeout=30)
        response.raise_for_status()
        rag_result = response.json()
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Could not reach RAG API: {error}") from error

    raw_answer = rag_result.get("answer", "")
    retrieved_context = rag_result.get("retrieved_context", [])

    # --- Output guardrail checks ---
    grounded = check_groundedness(raw_answer, retrieved_context)
    masked_answer, pii_masked = mask_pii(raw_answer)

    return {
        "answer": masked_answer,
        "flags": {
            "injection_detected": False,
            "grounded": grounded,
            "pii_masked": pii_masked,
        },
    }