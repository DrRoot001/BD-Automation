import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.resume import Resume
from app.schemas.resume import ResumeCreate, ResumeResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class ResumeUpdate(BaseModel):
    parsed_json: Optional[Dict[str, Any]] = None
    file_url: Optional[str] = None
    embedding: Optional[List[float]] = None


def _generate_resume_embedding_from_json(parsed: dict) -> Optional[List[float]]:
    if not parsed:
        return None

    summary = parsed.get("summary") or ""
    skills = ", ".join(parsed.get("skills") or [])
    keywords = ", ".join(parsed.get("keywords") or [])

    exp_texts = []
    for exp in parsed.get("experience") or []:
        title = exp.get("title") or ""
        company = exp.get("company") or ""
        desc = exp.get("description") or ""
        techs = ", ".join(exp.get("technologies") or [])
        exp_texts.append(f"{title} at {company}: {desc} (Tech: {techs})")
    experience = "\n".join(exp_texts)

    text_to_embed = f"Summary: {summary}\nSkills: {skills}\nKeywords: {keywords}\nExperience:\n{experience}"

    try:
        from module2.embedding.generator import generate_embedding
        embeddings = generate_embedding([text_to_embed])
        if embeddings and len(embeddings) > 0:
            return embeddings[0]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to auto-generate resume embedding: {e}")
    return None


@router.post("", response_model=ResumeResponse, status_code=201)
async def create_resume(resume: ResumeCreate, db: AsyncSession = Depends(get_db)):
    payload = resume.model_dump(exclude={"embedding"})
    embedding = resume.embedding
    if embedding is None and resume.parsed_json:
        # Run off the event loop: the embedding path can make a blocking
        # network call (OpenAI) that would otherwise freeze the whole server.
        embedding = await run_in_threadpool(
            _generate_resume_embedding_from_json, resume.parsed_json
        )

    # (candidate_id, version) is UNIQUE (resumes_candidate_id_version_key). The
    # M3 orchestrator computes the next version client-side (read max()+1), so
    # two tailored-resume writes for the SAME candidate racing in parallel (a
    # dynamic_apply run tailoring several jobs at once) can pick the same number
    # and one INSERT loses the unique race — surfacing to the operator as
    # "Failed to save tailored resume: duplicate key ... version=N already
    # exists". On that specific violation, recompute the next free version from
    # the DB and retry, serializing assignment at the single writer instead of
    # failing the whole pipeline step. Non-version violations re-raise as before.
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        db_resume = Resume(**payload)
        if embedding is not None:
            db_resume.embedding = embedding
        db.add(db_resume)
        try:
            await db.commit()
            await db.refresh(db_resume)
            return db_resume
        except IntegrityError as exc:
            await db.rollback()
            constraint = getattr(getattr(exc, "orig", None), "constraint_name", "") or str(exc)
            is_version_clash = "resumes_candidate_id_version" in constraint
            if not is_version_clash or attempt == max_attempts or payload.get("version") is None:
                raise
            next_v = (await db.execute(
                select(func.max(Resume.version)).where(
                    Resume.candidate_id == payload["candidate_id"]
                )
            )).scalar() or 0
            payload["version"] = next_v + 1
            logger.warning(
                f"[resumes] version clash for candidate={payload.get('candidate_id')} "
                f"(attempt {attempt}/{max_attempts}); retrying with version={payload['version']}"
            )


@router.patch("/{resume_id}", response_model=ResumeResponse)
async def update_resume(resume_id: str, update: ResumeUpdate, db: AsyncSession = Depends(get_db)):
    try:
        resume_uuid = uuid.UUID(resume_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid resume UUID")
    result = await db.execute(select(Resume).where(Resume.id == resume_uuid))
    db_resume = result.scalar_one_or_none()
    if not db_resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    for field, value in update.model_dump(exclude_none=True).items():
        setattr(db_resume, field, value)

    # Auto-regenerate embedding if parsed_json is updated and no explicit embedding is passed
    if "parsed_json" in update.model_dump(exclude_none=True) and update.embedding is None:
        # Run off the event loop: the embedding path can make a blocking
        # network call (OpenAI) that would otherwise freeze the whole server.
        db_resume.embedding = await run_in_threadpool(
            _generate_resume_embedding_from_json, db_resume.parsed_json
        )

    await db.commit()
    await db.refresh(db_resume)
    return db_resume


@router.get("/{resume_id}/view")
async def view_resume(resume_id: str, db: AsyncSession = Depends(get_db)):
    """Return a signed URL (or redirect) for viewing a resume PDF."""
    try:
        resume_uuid = uuid.UUID(resume_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid resume UUID")
    result = await db.execute(select(Resume).where(Resume.id == resume_uuid))
    db_resume = result.scalar_one_or_none()
    if not db_resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    file_url: str = db_resume.file_url or ""

    if not file_url:
        raise HTTPException(status_code=404, detail="No file URL stored for this resume. Please re-upload via the UI.")

    # For private Supabase buckets (/object/public/ URLs need signing)
    if "supabase.co/storage" in file_url and "/object/public/" in file_url:
        signed = await _create_supabase_signed_url(file_url)
        return RedirectResponse(url=signed, status_code=302)

    # Already a signed or direct https URL
    if file_url.startswith("http://") or file_url.startswith("https://"):
        return RedirectResponse(url=file_url, status_code=302)

    # Legacy local path
    if file_url.startswith("/files/"):
        import os
        from pathlib import Path

        from fastapi.responses import FileResponse

        # Determine the root directory of the backend
        backend_dir = Path(__file__).resolve().parent.parent.parent
        local_path = backend_dir / file_url.lstrip("/")

        if os.path.exists(local_path):
            return FileResponse(path=local_path, media_type="application/pdf", filename=f"resume_{resume_id}.pdf")

    raise HTTPException(
        status_code=404,
        detail="This resume was saved locally and is no longer accessible. Please re-upload via the UI."
    )


async def _create_supabase_signed_url(public_url: str, expires_in: int = 3600) -> str:
    """Exchange a Supabase public object URL for a signed URL (works for private buckets)."""
    import httpx

    from module3.utils.storage import SUPABASE_KEY, SUPABASE_URL

    if not SUPABASE_URL or not SUPABASE_KEY:
        return public_url

    # Extract bucket and object path from the public URL
    # Format: .../storage/v1/object/public/{bucket}/{path}
    try:
        after = public_url.split("/storage/v1/object/public/", 1)[1]
        bucket, *parts = after.split("/")
        object_path = "/".join(parts)
    except (IndexError, ValueError):
        return public_url

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{object_path}"
    headers = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(sign_url, json={"expiresIn": expires_in}, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                signed_path = data.get("signedURL") or data.get("signedUrl") or ""
                if signed_path:
                    base = SUPABASE_URL.rstrip("/")
                    return signed_path if signed_path.startswith("http") else f"{base}{signed_path}"
    except Exception:
        pass
    return public_url


@router.get("/{candidate_id}", response_model=List[ResumeResponse])
async def get_resumes(candidate_id: str, is_base: bool = None, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    query = select(Resume).where(Resume.candidate_id == candidate_uuid).order_by(Resume.version.asc())
    if is_base is not None:
        query = query.where(Resume.is_base == is_base)
    result = await db.execute(query)
    return result.scalars().all()
