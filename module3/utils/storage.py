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

SUPABASE_URL = get_env_var_from_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_URL") or get_env_var_from_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = get_env_var_from_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_ANON_KEY") or get_env_var_from_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_ANON_KEY")

async def upload_file_to_supabase(file_path: str, bucket_name: str, file_name: str) -> str:
    """
    Uploads a file to Supabase storage and returns the public URL.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[STORAGE] Warning: Supabase credentials not found. Falling back to local file path: {file_path}")
        return file_path
        
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket_name}/{file_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/pdf" # Assuming all these are PDFs
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
                return public_url
            elif resp.status_code == 400 and "Duplicate" in resp.text:
                # If it already exists, just return the public URL
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{file_name}"
                return public_url
            else:
                print(f"[STORAGE] Failed to upload to Supabase ({resp.status_code}): {resp.text}")
                return file_path
    except Exception as e:
        print(f"[STORAGE] Exception during Supabase upload: {e}")
        return file_path
