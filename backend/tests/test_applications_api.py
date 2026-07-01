import pytest
import sys
import uuid
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

from app.routers.auth import get_current_user
from app.models.user import User, UserRole

@pytest.fixture
def mock_db():
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_result
    
    mock_user = User(id=uuid.uuid4(), email="admin@test.com", role=UserRole.admin)
    
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield db
    app.dependency_overrides.clear()

def test_list_applications_filtering(mock_db):
    client = TestClient(app)
    
    candidate_id = str(uuid.uuid4())
    status = "FAILED"
    
    response = client.get(f"/api/applications?candidate_id={candidate_id}&status={status}")
    assert response.status_code == 200
    
    # Verify query was constructed correctly
    mock_db.execute.assert_called_once()
    args, _ = mock_db.execute.call_args
    query_str = str(args[0])
    
    assert "applications.candidate_id =" in query_str
    assert "applications.status =" in query_str

from unittest.mock import patch

@patch("app.routers.applications.publish_event")
@patch("app.tasks.browser_automation.execute_application")
def test_retry_application_endpoint(mock_execute, mock_publish, mock_db):
    client = TestClient(app)
    
    app_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    job_id = uuid.uuid4()
    
    # Mock Models
    from app.models.application import Application
    from app.models.job import Job
    from app.models.candidate import Candidate
    
    from datetime import datetime, timezone
    mock_app = Application(
        id=app_id,
        candidate_id=candidate_id,
        job_id=job_id,
        status="FAILED",
        retry_count=2,
        resume_id=None,
        cover_letter_url="http://cl.pdf",
        created_at=datetime.now(timezone.utc)
    )
    
    mock_job = Job(id=job_id, source="greenhouse", source_url="http://job.com", job_type="full-time")
    mock_candidate = Candidate(id=candidate_id, name="Alice")
    
    # Set up db.execute side effect for SELECT Application and SELECT ApplicationHistory
    mock_app_result = MagicMock()
    mock_app_result.scalar_one_or_none.return_value = mock_app
    
    mock_history_result = MagicMock()
    mock_history_result.scalars.return_value.first.return_value = None
    
    mock_db.execute.side_effect = [
        mock_app_result,      # SELECT Application
        mock_history_result,  # SELECT ApplicationHistory
    ]
    
    # Mock db.get
    async def mock_get(model, pk):
        if model == Job:
            return mock_job
        if model == Candidate:
            return mock_candidate
        return None
    mock_db.get.side_effect = mock_get
    
    response = client.post(f"/api/applications/{app_id}/retry")
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "QUEUED"
    assert data["retry_count"] == 0
    assert data["error_message"] is None
    assert data["failure_reason"] is None
    
    mock_db.commit.assert_called()
    mock_publish.assert_called_once()
    mock_execute.apply_async.assert_called_once()

from app.tasks.browser_automation import recover_stuck_applications

@patch("app.tasks.browser_automation.publish_status_changed")
@patch("app.database.task_session")
def test_recover_stuck_applications(mock_task_session, mock_publish):
    from datetime import datetime, timezone, timedelta
    from app.models.application import Application
    
    mock_session = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_task_session.return_value = mock_session_ctx
    
    now = datetime.now(timezone.utc)
    
    # QUEUED threshold is 60 min — 65 min should be caught
    stuck_app = Application(
        id=uuid.uuid4(),
        status="QUEUED",
        created_at=now - timedelta(minutes=65)
    )
    
    # 30 min QUEUED is NOT stuck (below 60-min threshold)
    recent_app = Application(
        id=uuid.uuid4(),
        status="QUEUED",
        created_at=now - timedelta(minutes=30)
    )
    
    # APPLICATION_STARTED threshold is 30 min — 35 min should be caught
    stuck_started = Application(
        id=uuid.uuid4(),
        status="APPLICATION_STARTED",
        created_at=now - timedelta(minutes=35)
    )
    
    # 20 min APPLICATION_STARTED is NOT stuck (threshold is 30 min)
    recent_started = Application(
        id=uuid.uuid4(),
        status="APPLICATION_STARTED",
        created_at=now - timedelta(minutes=20)
    )
    
    # First execute call: raw SQL JOIN query returns (id, status, last_updated) rows
    # The watchdog filters by threshold logic in Python
    raw_rows = [
        (stuck_app.id, "QUEUED", now - timedelta(minutes=65)),
        (recent_app.id, "QUEUED", now - timedelta(minutes=30)),
        (stuck_started.id, "APPLICATION_STARTED", now - timedelta(minutes=35)),
        (recent_started.id, "APPLICATION_STARTED", now - timedelta(minutes=20)),
    ]
    mock_raw_result = MagicMock()
    mock_raw_result.fetchall.return_value = raw_rows
    
    # Second execute call: ORM fetch of only the stuck apps (stuck_app and stuck_started)
    mock_orm_result = MagicMock()
    mock_orm_result.scalars.return_value.all.return_value = [stuck_app, stuck_started]
    
    mock_session.execute.side_effect = [
        mock_raw_result,   # Raw SQL JOIN query
        mock_orm_result,   # ORM fetch of stuck apps
    ]
    
    recover_stuck_applications()
    
    assert stuck_app.status == "FAILED"
    assert stuck_app.failure_reason == "INFRA_ERROR"
    assert stuck_app.error_message == "automation timeout — worker died or never picked up task"
    
    assert recent_app.status == "QUEUED"
    
    assert stuck_started.status == "FAILED"
    assert stuck_started.failure_reason == "INFRA_ERROR"
    assert stuck_started.error_message == "automation timeout — worker died or never picked up task"
    
    assert recent_started.status == "APPLICATION_STARTED"
    
    mock_session.commit.assert_called_once()
    assert mock_publish.call_count == 2


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_is_job_url_active_checker(mock_get):
    from app.browser_automation.services.executor import is_job_url_active
    
    # Case 1: 404 Not Found
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404
    mock_get.return_value = mock_resp_404
    assert await is_job_url_active("https://boards.greenhouse.io/company/jobs/12345") is False
    
    # Case 2: 200 OK
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.url = "https://boards.greenhouse.io/company/jobs/12345"
    mock_resp_200.text = "Apply for this job"
    mock_get.return_value = mock_resp_200
    assert await is_job_url_active("https://boards.greenhouse.io/company/jobs/12345") is True

    # Case 3: Redirect to home/careers
    mock_resp_redirect = MagicMock()
    mock_resp_redirect.status_code = 200
    mock_resp_redirect.url = "https://boards.greenhouse.io/company/"
    mock_resp_redirect.text = "Careers index page"
    mock_get.return_value = mock_resp_redirect
    assert await is_job_url_active("https://boards.greenhouse.io/company/jobs/12345") is False


@pytest.mark.asyncio
async def test_get_active_application_count_excludes_expired():
    from app.services.matching import get_active_application_count
    from app.models.application import Application
    
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result
    
    cand_id = uuid.uuid4()
    await get_active_application_count(cand_id, mock_session)
    
    mock_session.execute.assert_called_once()
    args, _ = mock_session.execute.call_args
    query_str = str(args[0])
    
    assert "failure_reason IS NULL" in query_str or "failure_reason !=" in query_str

