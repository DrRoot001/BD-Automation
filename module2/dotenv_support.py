from pathlib import Path
from dotenv import load_dotenv


def load_module2_dotenv() -> None:
    """Load environment variables from the backend/.env file if present."""
    env_path = Path(__file__).resolve().parents[1] / "backend" / ".env"
    if env_path.exists():
        load_dotenv(env_path)
