# VPS Deployment Guide for BD-Automator-Agent

This guide outlines how to deploy the **BD-Automator-Agent** application stack onto a Virtual Private Server (VPS) running Ubuntu 22.04 LTS (or similar Linux distributions) using Docker Compose, Nginx, and Let's Encrypt for SSL.

---

## 🏗️ Deployment Architecture

In production, the services are arranged as follows:
- **Next.js Frontend**: Port `3000` (internal only). Runs optimized production builds.
- **FastAPI Backend**: Port `8000` (internal only). Serves REST API and WebSockets.
- **PostgreSQL Database**: Port `5432` (internal only). Persistent storage.
- **Redis**: Port `6379` (internal only). Celery broker and cache.
- **Celery Worker**: Background task execution (Playwright, email scans, scraping).
- **Celery Beat**: Background scheduler for recurring tasks.
- **Nginx (Host)**: Reverse proxy on ports `80` (HTTP) and `443` (HTTPS) that routes traffic to the Frontend, Backend APIs, and WebSockets.

---

## 🖥️ System Requirements

- **Operating System**: Ubuntu 22.04 LTS (recommended) or Debian 12.
- **Hardware Sizing**:
  - **Minimum**: 2 vCPUs, 2 GB RAM (with a 2-4 GB Swap file).
  - **Recommended**: 2 vCPUs, 4 GB RAM or higher.
  > [!IMPORTANT]
  > Headless browser scraping (Playwright) and Next.js production builds are highly memory-intensive. Running them on a 1 GB or 2 GB server without a configured Swap file **will** cause random out-of-memory crashes.

---

## 🛠️ Step-by-Step Deployment

### Step 1: Initialize Swap Space (Crucial)

If your VPS has less than 4 GB of RAM, set up a Swap file to prevent out-of-memory issues during builds:

```bash
# Create a 4GB swap file
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make the swap permanent across reboots
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Verify swap is active
free -h
```

### Step 2: Install Docker and Docker Compose

Install Docker Engine and Docker Compose plugin on the VPS:

```bash
# Update package list and install prerequisites
sudo apt update && sudo apt install -y curl gnupg lsb-release ca-certificates apt-transport-https

# Add Docker's official GPG key
sudo fold=$(lsb_release -is | tr '[:upper:]' '[:lower:]')
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg

# Set up the repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Verify installation
docker compose version
```

### Step 3: Configure basic Firewall (UFW)

Secure your server by closing all ports except SSH, HTTP, and HTTPS:

```bash
# Allow default ports
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow http
sudo ufw allow https

# Enable firewall
sudo ufw enable
```

### Step 4: Clone & Configure the Repository

Clone your repository to the `/var/www/` or your home directory on the VPS:

```bash
cd /var/www
git clone https://github.com/YOUR_ORG/BD-Automator-Agent.git
cd BD-Automator-Agent
```

#### Set up Environment Variables
1. **Backend Configuration**: Create the backend production configuration.
   ```bash
   cp backend/.env.example backend/.env
   nano backend/.env
   ```
   Configure the following settings inside `backend/.env`:
   - `ENVIRONMENT=production`
   - `DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/bd_automator`
   - `REDIS_URL=redis://redis:6379/0`
   - Generate secure random secrets:
     - `SECRET_KEY`: Generate via `openssl rand -hex 32`
     - `ENCRYPTION_KEY`: Generate via `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - Fill in your API Credentials (`GEMINI_API_KEY`, Supabase URLs, Google client IDs, etc.).
   - Set `ENABLE_AUTO_SCRAPE=True` on your primary deployment instance to activate Celery Beat schedules.

2. **Frontend Configuration**: The frontend configuration is integrated in `docker-compose.prod.yml` and rewrites API requests via Nginx, so you don't need additional `.env` configurations except ensuring that public Supabase variables are set up if relevant.

---

### Step 5: Start the Container Stack

Run the production Docker Compose file:

```bash
# Build and run containers in detached mode
docker compose -f docker-compose.prod.yml up -d --build
```

#### Run Database Migrations
Once the database container is healthy, run the database migrations:

```bash
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
```

---

### Step 5b: Restore Database and Bucket Data (If Migrating)

If you are migrating existing candidates, jobs, resumes, and other application history data from your local backup to the new VPS stack, follow these steps:

#### 1. Transfer Backup Files to the VPS
Use `rsync` or `scp` to copy the database data dump and downloaded bucket files from your local machine to the VPS:

```bash
# Run this from your local machine (within the repo root):

# Create local_backup directory on the VPS
ssh user@your_vps_ip "mkdir -p /var/www/BD-Automator-Agent/local_backup"

# Copy database backup SQL dump to VPS
scp local_backup/database_data.sql user@your_vps_ip:/var/www/BD-Automator-Agent/local_backup/database_data.sql

# Copy downloaded storage bucket files recursively to VPS
rsync -avz local_backup/buckets/ user@your_vps_ip:/var/www/BD-Automator-Agent/local_backup/buckets/
```

#### 2. Import Database Data
With database migrations successfully run, import the SQL data dump into the active PostgreSQL container:

```bash
# Run this on your VPS inside the /var/www/BD-Automator-Agent directory:
docker exec -i bd-automator-agent-db-1 psql -U postgres -d bd_automator < local_backup/database_data.sql
```
> [!NOTE]
> - Change `bd-automator-agent-db-1` to match the exact database container name if it differs on your host (run `docker ps` to verify).
> - The import is wrapped in a single transaction and uses `SET session_replication_role = 'replica';` to bypass foreign key constraints, guaranteeing a clean and fast load.

#### 3. Restore Storage Buckets (Optional)
If your production stack continues to use Supabase Storage (connecting to your live Supabase buckets using the credentials in `backend/.env`), your files remain safe on Supabase and you do not need to perform any extra uploads.

However, if you are migrating to a local storage backend:
- Copy the contents of the `local_backup/buckets/` directory into the local storage volume/path configured in your application.

---

### Step 6: Setup Nginx Reverse Proxy

Nginx will intercept incoming web requests on port 80/443 and distribute them:
- Web traffic and pages go to Next.js (`127.0.0.1:3000`).
- `/api/...` endpoints go to FastAPI (`127.0.0.1:8000`).
- `/ws/...` websocket connections go to FastAPI with WebSocket upgrade headers.

1. Install Nginx:
   ```bash
   sudo apt update
   sudo apt install -y nginx
   ```

2. Create a new site configuration file:
   ```bash
   sudo nano /etc/nginx/sites-available/bd-automator
   ```

3. Paste the following configuration (replace `yourdomain.com` with your actual domain):

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    # Redirect all HTTP traffic to HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    # SSL configuration placeholder (Certbot will overwrite this block)
    ssl_certificate /etc/ssl/certs/ssl-cert-snakeoil.pem;
    ssl_certificate_key /etc/ssl/private/ssl-cert-snakeoil.key;

    # Increase maximum upload size (for resumes/PDFs)
    client_max_body_size 10M;

    # Proxy WebSocket connections (Celery live updates)
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Proxy API Requests directly to FastAPI
    location /api {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Proxy Frontend requests to Next.js
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

4. Enable the configuration and test Nginx:
   ```bash
   # Enable the site
   sudo ln -s /etc/nginx/sites-available/bd-automator /etc/nginx/sites-enabled/
   
   # Remove default nginx site to avoid conflicts
   sudo rm /etc/nginx/sites-enabled/default
   
   # Test configurations
   sudo nginx -t
   
   # Restart Nginx
   sudo systemctl restart nginx
   ```

---

### Step 7: Secure the Server with Let's Encrypt SSL

Install Certbot to manage free certificates:

```bash
sudo apt install -y certbot python3-certbot-nginx

# Obtain and configure the SSL certificate
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

Certbot will automatically verify ownership, fetch the certificate, and update the Nginx configuration to enable HTTP/2 and auto-redirect all traffic to HTTPS securely.

---

## 🔍 Maintenance & Monitoring

### Checking Application Logs
Use docker compose logs to debug issues inside containers:

```bash
# Check all logs
docker compose -f docker-compose.prod.yml logs -f

# Check only celery worker logs (ideal to watch Playwright/automation flows)
docker compose -f docker-compose.prod.yml logs -f celery_worker

# Check backend API logs
docker compose -f docker-compose.prod.yml logs -f api
```

### Restarting Services
When code is updated:

```bash
# Fetch latest changes
git pull origin main

# Rebuild and start container stack gracefully
docker compose -f docker-compose.prod.yml up -d --build

# Prune old images to save disk space
docker image prune -f
```

### Playwright Browser Performance
Playwright is configured inside the `celery_worker` using headless Chromium.
- By default, Playwright installs Chromium and required OS dependencies during the Docker build stage.
- For jobs that require cookies/session persistence, you can check that the tasks specify static userdata paths that point to Docker volumes to ensure browser states persist across scrapers.
- If Chromium encounters issues on the headless server, check that there are no CPU/RAM spikes using `htop` or `docker stats`.

---

## 🔒 Security Recommendations

1. **Change Default Postgres Credentials**:
   In `docker-compose.prod.yml` and `backend/.env`, replace the `postgres/postgres` credentials with a strong, generated password before launching the stack.
2. **Close Unused Ports**:
   Ensure ports `5432` (PostgreSQL), `6379` (Redis), `8000` (FastAPI), and `3000` (Next.js) are not exposed to the public internet. The `docker-compose.prod.yml` configuration achieves this by binding those ports only to `127.0.0.1`.
3. **Database Backups**:
   Set up a cron job to automatically backup the Postgres database volume to a secure off-site location:
   ```bash
   docker exec -t bd-automator-agent-db-1 pg_dumpall -c -U postgres > /path/to/backups/dump_$(date +%F).sql
   ```
