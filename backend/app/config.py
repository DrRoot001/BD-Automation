from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Load ALL .env entries into os.environ so external modules (module3, module4)
# can access them via os.getenv. pydantic-settings only loads declared fields.
_env_file = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=str(_env_file), override=False)


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str
    redis_url: str

    # ── Auth ──────────────────────────────────────────────────────────────────
    secret_key: str

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated in .env: "http://localhost:3000,https://app.example.com"
    allowed_origins: List[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ── Environment ───────────────────────────────────────────────────────────
    # "development" | "staging" | "production"
    environment: str = "development"

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_anon_key: str = ""
    supabase_admin_user_id: str = ""

    # ── Security: token encryption ────────────────────────────────────────────
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # ── Google OAuth ──────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""

    # ── LLM APIs (Gemini is primary) ─────────────────────────────────────────
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_api_key_2: str = ""
    groq_api_key: str = ""

    # ── Observability ─────────────────────────────────────────────────────────
    sentry_dsn: str = ""

    # ── Daily Match Settings ──────────────────────────────────────────────────
    daily_match_hour: int = 8
    max_daily_applications_per_candidate: int = 50
    job_matching_lookback_hours: int = 24
    job_matching_monday_lookback_hours: int = 72

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_matching_per_minute: int = 5
    rate_limit_apply_per_minute: int = 10

    # ── JWT ───────────────────────────────────────────────────────────────────
    access_token_expire_minutes: int = 60 * 24  # 1 day

    class Config:
        env_file = ".env"
        extra = "ignore"
        # Allow comma-separated string to parse into List[str]
        env_parse_none_str = "null"


@lru_cache()
def get_settings() -> Settings:
    return Settings()