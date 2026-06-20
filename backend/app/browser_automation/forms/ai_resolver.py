import json
import logging
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field

from .models import FormField
from module3.utils.gemini import generate_content_with_retry

logger = logging.getLogger(__name__)

class FieldResolution(BaseModel):
    label: str = Field(description="The exact label of the field as provided in the prompt")
    value: str = Field(description="The exact string to type, or the exact option value to select. Use empty string if it should be skipped.")
    reasoning: str = Field(description="Brief explanation of why this value was chosen based on the candidate profile")

class FormResolutionResult(BaseModel):
    fields: List[FieldResolution]

async def resolve_form_with_ai(
    fields: List[FormField],
    candidate_profile: Dict[str, Any],
    resume_text: Optional[str] = None
) -> Dict[str, str]:
    """
    Pass the entire form structure to the LLM along with the candidate profile.
    Returns a dictionary mapping field labels to their resolved values.
    """
    if not fields:
        return {}

    # Format the fields for the LLM
    fields_prompt = []
    for f in fields:
        field_info = f"- Label: '{f.label}' | Type: {f.field_type}"
        if f.options:
            field_info += f" | Options: {f.options}"
        if f.required:
            field_info += " | REQUIRED"
        fields_prompt.append(field_info)

    fields_str = "\n".join(fields_prompt)
    
    # Format the profile
    profile_str = json.dumps({k: v for k, v in candidate_profile.items() if not k.startswith("_")}, indent=2)

    prompt = (
        "You are an expert recruitment assistant AI. Your task is to fill out a job application form "
        "on behalf of the candidate based ONLY on their profile and resume data.\n\n"
        "Instructions:\n"
        "1. For each field provided, determine the best value to fill.\n"
        "2. If the field is a dropdown or radio button (has 'Options'), you MUST select EXACTLY one of the provided options. Do not invent options.\n"
        "3. If the field asks for demographic data (Gender, Race, Veteran status) and the profile doesn't specify, prefer 'Decline to self-identify' or similar if available.\n"
        "4. If you absolutely cannot determine the answer and the field is not required, return an empty string.\n\n"
        f"--- CANDIDATE PROFILE ---\n{profile_str}\n\n"
    )

    if resume_text:
        prompt += f"--- RESUME TEXT ---\n{resume_text[:2000]}\n\n"

    prompt += f"--- FORM FIELDS TO FILL ---\n{fields_str}\n"

    logger.info(f"[AI Resolver] Analyzing {len(fields)} fields with Gemini...")

    try:
        response = await generate_content_with_retry(
            contents=prompt,
            response_schema=FormResolutionResult,
            temperature=0.1,
            model="gemini-2.5-flash"
        )
        
        result_data = json.loads(response.text)
        resolution = FormResolutionResult(**result_data)
        
        # Build mapping
        resolved_map = {}
        for fr in resolution.fields:
            if fr.value:
                resolved_map[fr.label] = fr.value
                logger.debug(f"[AI Resolver] Resolved '{fr.label}' -> '{fr.value}' (Reason: {fr.reasoning})")
        
        return resolved_map

    except Exception as e:
        logger.error(f"[AI Resolver] Failed to resolve form: {e}")
        return {}
