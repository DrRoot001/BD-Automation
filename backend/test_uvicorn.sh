/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m uvicorn app.main:app --port 8003 > test.log 2> test_err.log &
UVICORN_PID=$!
sleep 4
curl -v -X POST http://127.0.0.1:8003/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@bdautomator.com", "password": "Admin123!"}'
sleep 2
kill $UVICORN_PID
cat test_err.log
