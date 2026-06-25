FROM python:3.10-slim

# Node.js (for the fetchfox scraper subprocess in app/module2/scraper)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node deps for the scraper sidecar
COPY app/module2/scraper/package.json app/module2/scraper/package.json
RUN cd app/module2/scraper && npm install --omit=dev

COPY . .

# Playwright needs its browser binaries installed for the scraper to work.
RUN cd app/module2/scraper && npx playwright install --with-deps chromium

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
