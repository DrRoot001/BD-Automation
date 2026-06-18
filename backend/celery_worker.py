"""
Celery worker process.
Run with: celery -A celery_worker worker --loglevel=info
"""

import os
from app.celery_app import celery_app

if __name__ == "__main__":
    # Configure Celery beat scheduler if needed
    celery_app.start()
