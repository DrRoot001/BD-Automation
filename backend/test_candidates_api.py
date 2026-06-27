import asyncio
from fastapi.testclient import TestClient
from app.main import app
from app.routers.auth import get_current_user
from app.models.user import User, UserRole
import uuid

# Override the get_current_user dependency
def override_get_current_user():
    return User(id=uuid.uuid4(), email="test@test.com", role=UserRole.admin)

app.dependency_overrides[get_current_user] = override_get_current_user

client = TestClient(app)

response = client.get("/api/candidates")
print(f"Status Code: {response.status_code}")
if response.status_code != 200:
    print(f"Error detail: {response.text}")
else:
    print("Success! Number of candidates returned:", len(response.json()))
