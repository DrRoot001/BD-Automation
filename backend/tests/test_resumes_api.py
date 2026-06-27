import pytest
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

# Ensure backend directory is in the path
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

from app.main import app
from app.database import get_db

@pytest.fixture
def mock_db():
    db = AsyncMock()
    # Mock refresh to assign ID and created_at
    from datetime import datetime, timezone
    async def mock_refresh(resume):
        resume.id = uuid.uuid4()
        resume.created_at = datetime.now(timezone.utc)
    db.refresh = mock_refresh
    
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.clear()

@patch("module2.embedding.generator.generate_embedding")
def test_create_resume_auto_generates_embedding(mock_generate_embedding, mock_db):
    mock_generate_embedding.return_value = [[0.1] * 1536]
    
    client = TestClient(app)
    payload = {
        "candidate_id": str(uuid.uuid4()),
        "version": 1,
        "file_url": "http://localhost:8000/files/resume.pdf",
        "parsed_json": {
            "summary": "Full Stack developer",
            "skills": ["Python", "FastAPI"],
            "keywords": ["developer"],
            "experience": []
        },
        "is_base": True
    }
    
    response = client.post("/api/resumes", json=payload)
    assert response.status_code == 201
    
    mock_generate_embedding.assert_called_once()
    assert "Full Stack developer" in mock_generate_embedding.call_args[0][0][0]
