"""Admin runtime-settings API.

Exposes the editable knobs in :mod:`app.services.runtime_config` so the operator
can change them from the admin panel while the system is running — the change
lands in shared Redis and every worker picks it up within a few seconds, no
restart. Admin-only.
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.routers.auth import require_admin
from app.services import runtime_config

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(_admin=Depends(require_admin)) -> Dict[str, Any]:
    """Editable runtime settings: UI metadata + current effective values + source."""
    return {"settings": runtime_config.describe()}


@router.put("")
async def update_settings(updates: Dict[str, Any], _admin=Depends(require_admin)) -> Dict[str, Any]:
    """Apply one or more setting overrides. Validated against the whitelist; takes
    effect live across all workers. Returns the full updated settings map."""
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="Body must be a non-empty {setting: value} object.")
    try:
        return {"settings": runtime_config.set_many(updates)}
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # Redis genuinely unreachable
        raise HTTPException(status_code=503, detail=f"Settings store unavailable: {exc}")
