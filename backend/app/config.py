from pydantic_settings import BaseSettings
from functools import lru_cache

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