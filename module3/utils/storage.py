import os
import re
import httpx
from typing import Optional


def safe_filename(filename: str, default: str = "file", extension: str = ".pdf") -> str:
    if not filename:
        filename = default
    filename = str(filename).strip()
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    filename = re.sub(r"_+", "_", filename)
    filename = filename.strip("._-")
    if not filename:
        filename = default
    if extension and not filename.lower().endswith(extension.lower()):
        filename += extension
    return filename


def get_env_var_from_file(filepath: str, var_name: str) -> Optional[str]:
    try:
        with open(filepath, "r") as f:
            for line in f:
                if line.startswith(f"{var_name}="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None

import os as _os
# Resolve the project root absolutely so this works regardless of CWD
_project_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_env_local = _os.path.join(_project_root, "frontend", ".env.local")
_env_fallback = _os.path.join(_project_root, "frontend", ".env")

SUPABASE_URL = (
    _os.getenv("SUPABASE_URL")
    or _os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    or get_env_var_from_file(_env_local, "NEXT_PUBLIC_SUPABASE_URL")
    or get_env_var_from_file(_env_fallback, "NEXT_PUBLIC_SUPABASE_URL")
)
SUPABASE_KEY = (
    _os.getenv("SUPABASE_KEY")
    or _os.getenv("SUPABASE_ANON_KEY")
    or _os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    or get_env_var_from_file(_env_local, "NEXT_PUBLIC_SUPABASE_ANON_KEY")
    or get_env_var_from_file(_env_fallback, "NEXT_PUBLIC_SUPABASE_ANON_KEY")
)

async def upload_file_to_supabase(file_path: str, bucket_name: str, file_name: str, clean_local: bool = True) -> str:
    """
    Uploads a file to Supabase storage and returns the public URL.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[STORAGE] Warning: Supabase credentials not found. Falling back to local file storage.")
        try:
            dest_dir = os.path.join(_project_root, "backend", "files", bucket_name)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, file_name)
            import shutil
            shutil.copy2(file_path, dest_path)
            print(f"[STORAGE] Successfully saved file locally to {dest_path}")
            if clean_local:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception:
                    pass
            return f"/files/{bucket_name}/{file_name}"
        except Exception as e:
            print(f"[STORAGE] Failed to save file locally: {e}")
            return file_path

        
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket_name}/{file_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/pdf",
    }
    
    print(f"[STORAGE] Uploading {file_path} to Supabase bucket '{bucket_name}'...")
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=file_data)
            
            if resp.status_code in (200, 201):
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{file_name}"
                print(f"[STORAGE] Successfully uploaded to {public_url}")
                if clean_local:
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            print(f"[STORAGE] Cleaned up local file: {file_path}")
                    except Exception as cleanup_err:
                        print(f"[STORAGE] Non-fatal error cleaning up local file {file_path}: {cleanup_err}")
                return public_url
            elif resp.status_code == 400 and "Duplicate" in resp.text:
                # If it already exists, just return the public URL
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{file_name}"
                if clean_local:
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            print(f"[STORAGE] Cleaned up local file (duplicate): {file_path}")
                    except Exception as cleanup_err:
                        print(f"[STORAGE] Non-fatal error cleaning up local file {file_path}: {cleanup_err}")
                return public_url
            else:
                print(f"[STORAGE] Failed to upload to Supabase ({resp.status_code}): {resp.text}")
                return file_path
    except Exception as e:
        print(f"[STORAGE] Exception during Supabase upload: {e}")
        return file_path
