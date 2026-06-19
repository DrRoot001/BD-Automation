from pydantic import BaseModel
from typing import List, Literal, Optional, Dict

class FormField(BaseModel):
    selector: str
    field_type: Literal["text", "email", "phone", "select", "radio", "checkbox", "file", "textarea", "date", "url"]
    label: str
    required: bool
    options: Optional[List[str]] = None
    value: Optional[str] = None

class DetectedForm(BaseModel):
    form_type: Literal["EASY_APPLY", "EXTERNAL_FORM", "EMAIL", "UNKNOWN"]
    fields: List[FormField]
    steps: int
    current_step: int
    has_captcha: bool
    captcha_type: Optional[str] = None
    has_file_upload: bool
    submit_selector: Optional[str] = None

class FieldMapping(BaseModel):
    form_selector: str
    candidate_field: str
    value: str
