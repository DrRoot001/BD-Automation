"""Lightweight SQLite job store for normalized jobs."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional
from datetime import datetime
import json

DB_PATH = Path(__file__).parent / "jobs.db"

class JobStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.conn = sqlite3.connect(str(self.db_path))
        self._ensure_schema()

    def _ensure_schema(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                canonical_url TEXT,
                posted_at TEXT,
                inserted_at TEXT,
                salary_min INTEGER,
                salary_max INTEGER,
                pay_period TEXT,
                skills TEXT,
                source TEXT
            )
            """
        )
        self.conn.commit()

    def insert_job(self, job):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO jobs (id,title,company,location,url,canonical_url,posted_at,inserted_at,salary_min,salary_max,pay_period,skills,source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.job_id,
                job.title,
                job.company,
                job.location,
                job.url,
                job.canonical_url,
                job.posted_at.isoformat() if job.posted_at else None,
                datetime.utcnow().isoformat(),
                job.salary_min,
                job.salary_max,
                job.pay_period,
                json.dumps(job.skills or []),
                job.source,
            ),
        )
        self.conn.commit()

    def get_job(self, job_id: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    def list_jobs(self, limit: int = 100):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM jobs ORDER BY inserted_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]
