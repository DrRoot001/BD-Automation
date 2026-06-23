import pytest
import sys
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
from app.routers.auth import require_admin
from app.models.user import User, UserRole

# Define a mock admin user and a list of users
mock_admin = User(
    id="00000000-0000-0000-0000-000000000000",
    supabase_user_id="admin-supabase-id",
    email="admin@bdautomator.com",
    role=UserRole.admin,
    full_name="Sabih Admin",
    created_at=datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc),
)

mock_bd_user = User(
    id="11111111-1111-1111-1111-111111111111",
    supabase_user_id="bd-supabase-id",
    email="bd_user@bdautomator.com",
    role=UserRole.bd_user,
    full_name="Sabih BD",
    created_at=datetime(2026, 6, 23, 13, 0, 0, tzinfo=timezone.utc),
)

@pytest.fixture
def client_with_overrides():
    # Mock database session
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_admin, mock_bd_user]
    mock_db.execute.return_value = mock_result

    # Dependency overrides
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[require_admin] = lambda: mock_admin

    client = TestClient(app)
    yield client

    # Clean up overrides
    app.dependency_overrides.clear()

def test_list_users_includes_created_at(client_with_overrides):
    response = client_with_overrides.get("/api/auth/admin/users")
    assert response.status_code == 200
    
    data = response.json()
    assert len(data) == 2
    
    # Check that created_at is returned and formats correctly
    user_admin = [u for u in data if u["id"] == str(mock_admin.id)][0]
    assert user_admin["created_at"] == "2026-06-23T12:00:00+00:00"
    
    user_bd = [u for u in data if u["id"] == str(mock_bd_user.id)][0]
    assert user_bd["created_at"] == "2026-06-23T13:00:00+00:00"
