"""Salary and pay period parser.

Extracts salary information from raw text and normalizes to min/max integers.
Handles hourly, yearly, and range formats.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


def parse_salary(salary_text: Optional[str]) -> Tuple[Optional[int], Optional[int], str]:
    """Parse salary text and extract min/max values and pay period.
    
    Args:
        salary_text: Raw salary text (e.g., "$120k - $150k", "$60/hour", "competitive").
    
    Returns:
        Tuple of (salary_min, salary_max, pay_period) where pay_period is "hourly" or "yearly".
        Returns (None, None, "yearly") if salary cannot be parsed.
    
    Examples:
        "$60/hour" → (60, None, "hourly")
        "$120k - $150k" → (120000, 150000, "yearly")
        "$80 per hour" → (80, None, "hourly")
        "competitive" → (None, None, "yearly")
    """
    if not salary_text:
        return None, None, "yearly"
    
    salary_text = salary_text.strip()
    
    # Detect pay period
    is_hourly = any(keyword in salary_text.lower() for keyword in [
        "/hour", "per hour", "hourly", "/hr", "per hr", "$xx/hour"
    ])
    pay_period = "hourly" if is_hourly else "yearly"
    
    # Extract numbers and currency amounts
    # Pattern: $60, $120,000, 60, 120000 (in various formats)
    pattern = r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
    
    amounts = []
    seen = set()  # Avoid duplicates
    
    matches = re.findall(pattern, salary_text)
    for match in matches:
        # Clean up: remove commas, decimals
        amount = match.replace(",", "").replace(".", "")
        try:
            amount_int = int(amount)
            # Avoid adding the same amount twice
            if amount_int not in seen:
                amounts.append(amount_int)
                seen.add(amount_int)
        except ValueError:
            continue
    
    if not amounts:
        return None, None, pay_period
    
    # Handle 'k' suffix (e.g., "120k" means 120,000)
    if "k" in salary_text.lower() or "K" in salary_text:
        amounts = [a * 1000 if a < 1000 else a for a in amounts]
    
    # Sort amounts
    amounts.sort()
    
    if len(amounts) == 1:
        # Single amount: return only min, no max
        return amounts[0], None, pay_period
    else:
        # Multiple amounts: return min and max
        return amounts[0], amounts[-1], pay_period


def format_salary(salary_min: Optional[int], salary_max: Optional[int], pay_period: str) -> str:
    """Format salary for display.
    
    Args:
        salary_min: Minimum salary.
        salary_max: Maximum salary.
        pay_period: "hourly" or "yearly".
    
    Returns:
        Formatted salary string.
    
    Examples:
        (60, None, "hourly") → "$60/hour"
        (120000, 150000, "yearly") → "$120k - $150k/year"
    """
    if salary_min is None:
        return "Salary not provided"
    
    if pay_period == "hourly":
        if salary_max and salary_max != salary_min:
            return f"${salary_min} - ${salary_max}/hour"
        return f"${salary_min}/hour"
    else:  # yearly
        if salary_max and salary_max != salary_min:
            min_k = salary_min // 1000
            max_k = salary_max // 1000
            return f"${min_k}k - ${max_k}k/year"
        min_k = salary_min // 1000
        return f"${min_k}k/year"
