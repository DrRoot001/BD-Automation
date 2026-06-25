from pathlib import Path
from dotenv import load_dotenv
import os

# Load environment variables from backend/.env so modules using os.getenv() work.
try:
	env_path = Path(__file__).resolve().parents[1] / ".env"
	if env_path.exists():
		load_dotenv(env_path)
except Exception:
	# If dotenv isn't available or load fails, continue without raising.
	pass

# Expose package-level names if needed
__all__ = []
