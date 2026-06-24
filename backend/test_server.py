import subprocess
import requests
import time
import sys

p = subprocess.Popen(["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3", "-m", "uvicorn", "app.main:app", "--port", "8005"], stderr=subprocess.PIPE, text=True)
time.sleep(3)
try:
    response = requests.post("http://127.0.0.1:8005/api/auth/login", json={
        "email": "admin@bdautomator.com",
        "password": "Admin123!"
    })
    print("STATUS:", response.status_code)
    print("BODY:", response.text)
finally:
    p.terminate()
    out, err = p.communicate()
    print("UVICORN STDERR:", err)
