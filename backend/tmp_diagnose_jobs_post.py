from fastapi.testclient import TestClient
from app.main import app
from pprint import pprint
import uuid

client = TestClient(app)

job = {
    'title': 'Diagnose Greenhouse Post ' + str(uuid.uuid4()),
    'company': 'Anthropic',
    'location': 'Remote',
    'source': 'greenhouse',
    'source_url': 'https://job-boards.greenhouse.io/anthropic/jobs/' + str(uuid.uuid4()),
    'description': 'Test job for backend diagnostics',
    'skills': ['ai', 'ml'],
    'salary_min': 120000,
    'pay_period': 'yearly',
    'job_type': 'full-time',
    'embedding': [0.1] * 1536,
}

response = client.post('/api/jobs', json=[job])
print('status_code=', response.status_code)
print('text=')
print(response.text)
print('json=')
try:
    pprint(response.json())
except Exception as exc:
    print('json parse error:', exc)
