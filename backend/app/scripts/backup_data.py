import os
import sys
import shutil
import asyncio
import json
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from supabase import create_client

# Add project root to python path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(project_root)

# Load environment
load_dotenv(dotenv_path=os.path.join(project_root, "backend", ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

BACKUP_DIR = os.path.join(project_root, "local_backup")
BUCKETS_DIR = os.path.join(BACKUP_DIR, "buckets")

TABLES_ORDER = [
    "users",
    "candidates",
    "companies",
    "jobs",
    "resumes",
    "applications",
    "application_history",
    "emails",
    "interviews",
    "cover_letters",
    "alembic_version"
]

def format_value(val, col_name):
    if val is None:
        return "NULL"
    if col_name == "embedding":
        # Format as vector
        if isinstance(val, str):
            return f"'{val}'::vector"
        return f"'{str(list(val))}'::vector"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        # Array formatting
        escaped_items = []
        for item in val:
            if item is None:
                escaped_items.append("NULL")
            else:
                escaped_item = str(item).replace("'", "''")
                escaped_items.append(f"'{escaped_item}'")
        return f"ARRAY[{', '.join(escaped_items)}]::text[]"
    if isinstance(val, dict):
        json_str = json.dumps(val)
        escaped_json = json_str.replace("'", "''")
        return f"'{escaped_json}'::jsonb"
    if isinstance(val, datetime):
        return f"'{val.isoformat()}'"
    escaped_str = str(val).replace("'", "''")
    return f"'{escaped_str}'"

async def backup_database():
    print("--- Starting Database Backup ---")
    if not DATABASE_URL:
        print("Error: DATABASE_URL is not set!")
        return False
    
    print(f"Connecting to database: {DATABASE_URL.split('@')[-1]}")
    engine = create_async_engine(
        DATABASE_URL,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    )
    
    sql_lines = []
    sql_lines.append("-- BD-Automator Local Database Backup")
    sql_lines.append(f"-- Generated on: {datetime.now().isoformat()}\n")
    sql_lines.append("BEGIN;")
    sql_lines.append("SET session_replication_role = 'replica';\n")
    
    async with engine.connect() as conn:
        # Disable statement timeout for this connection session
        try:
            await conn.execute(text("SET statement_timeout = 0"))
            print("Successfully set statement_timeout = 0 for the session.")
        except Exception as timeout_err:
            print(f"Warning: Could not set statement_timeout = 0: {timeout_err}")

        for table in TABLES_ORDER:
            print(f"Exporting table: {table}...")
            # Use a savepoint or separate execute block to avoid aborting the entire connection
            try:
                # Query row count
                res_count = await conn.execute(text(f"SELECT COUNT(*) FROM \"{table}\""))
                count = res_count.scalar()
                print(f"  Table '{table}' has {count} rows.")
                
                if count == 0:
                    continue
                
                # Fetch first row to get columns
                res_cols = await conn.execute(text(f"SELECT * FROM \"{table}\" LIMIT 1"))
                columns = list(res_cols.keys())
                cols_str = ", ".join(f'"{col}"' for col in columns)
                
                sql_lines.append(f"-- Data for table: {table}")
                sql_lines.append(f"TRUNCATE TABLE \"{table}\" CASCADE;")
                
                # Fetch in batches to prevent statement timeout and high memory usage
                batch_size = 500
                offset = 0
                while offset < count:
                    print(f"  Fetching rows {offset} to {offset + batch_size}...")
                    batch_res = await conn.execute(text(
                        f"SELECT * FROM \"{table}\" ORDER BY 1 LIMIT {batch_size} OFFSET {offset}"
                    ))
                    batch_rows = batch_res.all()
                    if not batch_rows:
                        break
                        
                    for row in batch_rows:
                        vals = []
                        for col_name, val in zip(columns, row):
                            vals.append(format_value(val, col_name))
                        vals_str = ", ".join(vals)
                        sql_lines.append(f"INSERT INTO \"{table}\" ({cols_str}) VALUES ({vals_str});")
                        
                    offset += len(batch_rows)
                sql_lines.append("")
                print(f"  Successfully exported {offset} rows from '{table}'")
            except Exception as e:
                print(f"Error exporting table '{table}': {e}")
                
    sql_lines.append("SET session_replication_role = 'origin';")
    sql_lines.append("COMMIT;")
    
    # Save SQL file
    os.makedirs(BACKUP_DIR, exist_ok=True)
    sql_file = os.path.join(BACKUP_DIR, "database_data.sql")
    with open(sql_file, "w") as f:
        f.write("\n".join(sql_lines))
    print(f"Database data successfully saved to: {sql_file}")
    
    # Copy schema file
    src_schema = os.path.join(project_root, "backend", "app", "models", "schema.sql")
    dest_schema = os.path.join(BACKUP_DIR, "database_schema.sql")
    if os.path.exists(src_schema):
        shutil.copy(src_schema, dest_schema)
        print(f"Database schema copied to: {dest_schema}")
    else:
        print(f"Warning: schema.sql not found at {src_schema}")
        
    await engine.dispose()
    return True

def download_bucket_recursive(supabase, bucket_id, prefix=""):
    offset = 0
    limit = 100
    files_downloaded = 0
    
    while True:
        try:
            items = supabase.storage.from_(bucket_id).list(
                prefix,
                options={"limit": limit, "offset": offset, "sortBy": {"column": "name", "order": "asc"}}
            )
        except Exception as e:
            print(f"Error listing {bucket_id}/{prefix}: {e}")
            break
            
        if not items:
            break
            
        for item in items:
            name = item.get("name")
            if not name:
                continue
            item_path = f"{prefix}/{name}".strip("/") if prefix else name
            is_folder = item.get("id") is None
            
            if is_folder:
                files_downloaded += download_bucket_recursive(supabase, bucket_id, item_path)
            else:
                dest_path = os.path.join(BUCKETS_DIR, bucket_id, item_path)
                
                # Check if it already exists with matching size
                meta = item.get("metadata")
                size = meta.get("size") if meta else None
                if os.path.exists(dest_path) and (size is None or os.path.getsize(dest_path) == size):
                    # File exists and size matches, skip download
                    files_downloaded += 1
                    continue
                
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                print(f"Downloading {bucket_id}/{item_path} -> {dest_path}")
                try:
                    data = supabase.storage.from_(bucket_id).download(item_path)
                    with open(dest_path, "wb") as f:
                        f.write(data)
                    files_downloaded += 1
                except Exception as ex:
                    print(f"Failed to download {bucket_id}/{item_path}: {ex}")
                    
        if len(items) < limit:
            break
        offset += limit
        
    return files_downloaded

async def backup_buckets():
    print("\n--- Starting Supabase Storage Backup ---")
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Error: Supabase credentials are not set!")
        return False
        
    print(f"Connecting to Supabase Storage: {SUPABASE_URL}")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    try:
        buckets = supabase.storage.list_buckets()
        print(f"Found {len(buckets)} buckets.")
        os.makedirs(BUCKETS_DIR, exist_ok=True)
        
        for bucket in buckets:
            print(f"Backing up bucket '{bucket.id}'...")
            downloaded = download_bucket_recursive(supabase, bucket.id)
            print(f"Downloaded {downloaded} files from bucket '{bucket.id}'")
            
    except Exception as e:
        print(f"Error backing up buckets: {e}")
        return False
    return True

async def main():
    db_ok = await backup_database()
    storage_ok = await backup_buckets()
    if db_ok and storage_ok:
        print("\n=== Backup Completed Successfully! ===")
    else:
        print("\n=== Backup Failed or Had Errors ===")

if __name__ == "__main__":
    asyncio.run(main())
