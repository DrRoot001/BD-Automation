# Stage 1: Builder
# Build context is the REPO ROOT (so module2-5 are available alongside backend/)
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Production
FROM python:3.12-slim AS production

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# App lives at /app — we copy the entire repo root here so module2-5 are siblings of backend/
WORKDIR /app

# Install system dependencies required for runtime, Playwright, and Node.js
RUN apt-get update && apt-get install -y \
    libpq5 \
    wget \
    gnupg \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python packages from builder
COPY --from=builder /install /usr/local

# Install Playwright browser dependencies (specifically Chromium)
RUN playwright install --with-deps chromium

# Copy the entire repo (context = repo root) so module2, module3, module4, module5
# are available next to backend/ — main.py inserts repo root into sys.path
COPY . .

# Install Node dependencies for the scraper module
RUN npm install --prefix module2/scraper

# Install root pyproject.toml so all local packages (module2-5, backend) are importable
RUN pip install --no-deps .

# FastAPI runs from the backend/ subdirectory context; uvicorn discovers app.main:app
WORKDIR /app/backend

# Default command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
