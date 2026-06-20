from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv

# Load ALL .env entries into os.environ so external modules (module3, module4)
# can access them via os.getenv. pydantic-settings only loads declared fields.
_env_file = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=str(_env_file), override=False)


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    secret_key: str
    google_client_id: str = ""
    google_client_secret: str = ""


    # JWT
    access_token_expire_minutes: int = 60 * 24  # 1 day

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()