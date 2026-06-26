import requests
import time
import subprocess
import sys

p = subprocess.Popen(["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3", "-m", "uvicorn", "app.main:app"], stderr=subprocess.PIPE, text=True)
time.sleep(3)
try:
    response = requests.post("http://127.0.0.1:8000/api/auth/admin/create_bd_user", json={
        "name": "Admin User",
        "email": "admin@bdautomator.com",
        "password": "Admin123!",
        "role": "admin"
    })
    print("STATUS:", response.status_code)
    print("BODY:", response.text)
finally:
    p.kill()
    out, err = p.communicate()
    print("UVICORN STDERR:", err)
