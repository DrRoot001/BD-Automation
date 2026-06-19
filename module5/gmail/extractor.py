"""Extract structured interview details from an email using Gemini."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional, Literal
from datetime import datetime
from pydantic import BaseModel

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract structured interview details from this email.
Return valid JSON only. Use null for fields you cannot find.

{{"company": "string", "position": "string", "interview_date": "ISO 8601 or null",
  "interview_type": "phone|video|onsite|assessment", "meeting_url": "string or null",
  "interviewer_name": "string or null", "additional_notes": "string"}}

Subject: {subject}
Body:
{body_text}"""


class InterviewDetails(BaseModel):
    company: str = ""
    position: str = ""
    interview_date: Optional[str] = None   # ISO string or None
    interview_type: Literal["phone", "video", "onsite", "assessment"] = "phone"
    meeting_url: Optional[str] = None
    interviewer_name: Optional[str] = None
    additional_notes: str = ""


def _extract_meeting_url(text: str) -> Optional[str]:
    """Regex fallback to find Zoom/Meet/Teams links."""
    patterns = [
        r"https://[\w.]+\.zoom\.us/\S+",
        r"https://meet\.google\.com/\S+",
        r"https://teams\.microsoft\.com/\S+",
        r"https://[\w.]+\.webex\.com/\S+",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            url = match.group(0).rstrip(".,)")
            return url
    return None


async def extract_interview_details(subject: str, body_text: str) -> InterviewDetails:
    """Use Gemini to extract interview metadata from an email."""
    from module3.utils.gemini import generate_content_with_retry

    prompt = EXTRACTION_PROMPT.format(subject=subject, body_text=body_text[:4000])

    try:
        response = await generate_content_with_retry(
            contents=prompt,
            temperature=0.1,
            response_mime_type="application/json",
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)

        # Regex fallback for meeting URL
        meeting_url = data.get("meeting_url") or _extract_meeting_url(body_text)

        return InterviewDetails(
            company=data.get("company", ""),
            position=data.get("position", ""),
            interview_date=data.get("interview_date"),
            interview_type=data.get("interview_type", "phone"),
            meeting_url=meeting_url,
            interviewer_name=data.get("interviewer_name"),
            additional_notes=data.get("additional_notes", ""),
        )
    except Exception as e:
        logger.error(f"[Extractor] Failed: {e}")
        meeting_url = _extract_meeting_url(body_text)
        return InterviewDetails(meeting_url=meeting_url)
