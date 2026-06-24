import pytest
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
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
    # Mock return values for db execution
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    db.execute.return_value = mock_result
    
    # Refresh sets generated ID and timestamps to bypass Pydantic validation
    async def mock_refresh(job):
        job.id = uuid.uuid4()
        job.created_at = datetime.now(timezone.utc)
    db.refresh = mock_refresh
    
    # Overwrite get_db
    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.clear()

def test_create_jobs_endpoint(mock_db):
    client = TestClient(app)
    
    payload = [
      {
        "title": "React Developer",
        "company": "TechCorp",
        "location": "Remote, US",
        "source": "linkedin",
        "source_url": "https://linkedin.com/jobs/123",
        "description": "Building beautiful React apps",
        "skills": ["React", "TypeScript", "Tailwind"]
      },
      {
        "title": "FastAPI Engineer",
        "company": "DataSoft",
        "location": "New York",
        "source": "glassdoor",
        "source_url": "https://glassdoor.com/jobs/456",
        "description": "Python API development",
        "skills": ["Python", "FastAPI", "Docker"]
      }
    ]
    
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 201
    
    data = response.json()
    assert len(data) == 2
    assert data[0]["title"] == "React Developer"
    assert data[1]["title"] == "FastAPI Engineer"
    assert "id" in data[0]
    assert "created_at" in data[0]
    
    mock_db.commit.assert_called_once()
