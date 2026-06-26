import requests

try:
    response = requests.post("http://127.0.0.1:8002/api/auth/login", json={
        "email": "admin@bdautomator.com",
        "password": "Admin123!"
    })
    print("STATUS:", response.status_code)
    print("BODY:", response.text)
except Exception as e:
    print("ERR:", e)
