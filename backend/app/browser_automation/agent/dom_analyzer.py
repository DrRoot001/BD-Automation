"""Python-side DOM semantic analysis and enrichment.

Takes the raw JS DOM snapshot and enriches it with semantic classification
so the LLM has a structured understanding of each field's purpose.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class DomAnalyzer:
    """Enriches the raw JS DOM snapshot with semantic classification and confidence scores."""

    @classmethod
    def analyze(cls, fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Process the raw list of fields and add semantic types."""
        # Simple duplicate detection to flag likely duplicate fields
        seen_labels = {}

        for field in fields:
            label = field.get("label", "")
            lower_label = label.lower()
            field_type = field.get("type", "").lower()
            
            # Semantic classification
            semantic_type = cls._classify_semantic_type(lower_label, field_type)
            if semantic_type:
                field["semantic_type"] = semantic_type
                
            # Duplicate field detection
            # Ignore very short labels or common buttons
            if len(lower_label) > 3 and field_type not in ("button", "submit"):
                if lower_label in seen_labels:
                    field["duplicate_of_previous"] = True
                else:
                    seen_labels[lower_label] = True

        return fields

    @classmethod
    def _classify_semantic_type(cls, label: str, field_type: str) -> Optional[str]:
        if not label:
            return None
            
        # Prioritize exact/strong matches
        if re.search(r'\b(first name|last name|full name)\b', label) or label == 'name':
            return "name"
        if re.search(r'\b(email|e-mail)\b', label):
            return "email"
        if re.search(r'\b(phone|telephone|mobile)\b', label):
            return "phone"
        if re.search(r'\b(resume|cv|curriculum vitae)\b', label):
            return "resume"
        if re.search(r'\b(cover letter)\b', label):
            return "cover_letter"
        if re.search(r'\b(linkedin|github|website|portfolio)\b', label):
            return "link"
        if re.search(r'\b(address|city|state|zip|postal|country)\b', label):
            return "address"
        if re.search(r'\b(authorized|authorization|sponsorship|visa)\b', label):
            return "work_auth"
        if re.search(r'\b(gender|sex|race|ethnicity|veteran|disability)\b', label):
            return "demographic"
        if re.search(r'\b(salary|compensation|pay|rate)\b', label):
            return "salary"
        if re.search(r'\b(date|start date|availability)\b', label) or field_type == "date":
            return "date"
            
        if field_type in ("checkbox", "radio", "combobox", "select", "text", "textarea"):
            return "screening_question"
            
        return None
