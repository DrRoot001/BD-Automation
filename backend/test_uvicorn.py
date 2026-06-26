import subprocess
import time
import requests

p = subprocess.Popen(
    ["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3", "-m", "uvicorn", "app.main:app", "--port", "8009"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)
time.sleep(3)

try:
    res = requests.post("http://127.0.0.1:8009/api/auth/login", json={"email": "admin@bdautomator.com", "password": "Admin123!"})
    print("STATUS:", res.status_code)
except Exception as e:
    print("REQ_ERR:", e)

time.sleep(1)
p.terminate()
out, err = p.communicate()
print("STDOUT:", out)
print("STDERR:", err)
