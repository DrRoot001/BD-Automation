"""LLM-based email classifier using Gemini."""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)

EmailCategory = Literal[
    "APPLIED_CONFIRMATION", "INTERVIEW_R1", "INTERVIEW_R2",
    "ASSESSMENT", "REJECTED", "OFFER", "UNKNOWN"
]

CLASSIFICATION_PROMPT = """Classify this recruitment email into exactly one category:
- APPLIED_CONFIRMATION: Application was received/acknowledged by the employer
- INTERVIEW_R1: First round interview invitation (phone screen, recruiter call, initial screening)
- INTERVIEW_R2: Later round interview (technical, panel, hiring manager, coding assessment follow-up)
- ASSESSMENT: Online assessment, take-home assignment, or coding challenge invitation
- REJECTED: Application was rejected or they decided not to move forward
- OFFER: Job offer extended to the candidate
- UNKNOWN: Cannot determine with confidence (newsletters, unrelated, ambiguous)

Respond with valid JSON only:
{{"classification": "...", "confidence": 0.0, "reasoning": "one sentence"}}

Subject: {subject}
From: {from_addr}
Body (first 3000 chars):
{body_text}"""


class ClassificationResult(BaseModel):
    classification: EmailCategory = "UNKNOWN"
    confidence: float = 0.0
    reasoning: str = ""


async def classify_email(
    subject: str,
    from_addr: str,
    body_text: str,
) -> ClassificationResult:
    """Call Gemini to classify a recruitment email."""
    from module3.utils.gemini import generate_content_with_retry

    prompt = CLASSIFICATION_PROMPT.format(
        subject=subject,
        from_addr=from_addr,
        body_text=body_text[:3000],
    )

    try:
        response = await generate_content_with_retry(
            contents=prompt,
            temperature=0.1,
            response_mime_type="application/json",
        )
        raw = response.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        result = ClassificationResult(
            classification=data.get("classification", "UNKNOWN"),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
        )
        logger.info(f"[Classifier] {result.classification} ({result.confidence:.2f}) — {result.reasoning[:80]}")
        return result
    except Exception as e:
        logger.error(f"[Classifier] Failed: {e}")
        return ClassificationResult(classification="UNKNOWN", confidence=0.0, reasoning=str(e))
