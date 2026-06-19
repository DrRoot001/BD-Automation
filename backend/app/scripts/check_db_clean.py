import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from dotenv import load_dotenv

# Load env variables from backend/.env
env_path = "/Users/sabihhaider/Documents/BD-Automator-Agent/backend/.env"
load_dotenv(dotenv_path=env_path)

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

async def check_cleanliness():
    if not DATABASE_URL:
        print("DATABASE_URL is not set!")
        return

    engine = create_async_engine(DATABASE_URL)
    
    tables_to_check = ["candidates", "jobs", "resumes", "applications"]
    
    async with engine.connect() as conn:
        print("--- DATABASE CLEANLINESS CHECK ---")
        for table in tables_to_check:
            print(f"\nChecking table '{table}' for keys: 'test', 'TEST', 'mock', '999'...")
            
            # Get columns to build query dynamically
            cols_query = text(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table}'")
            cols_res = await conn.execute(cols_query)
            cols = cols_res.fetchall()
            
            conditions = []
            params = {}
            param_idx = 1
            
            for col_name, data_type in cols:
                # Text/Varchar columns
                if data_type in ("character varying", "text", "character"):
                    conditions.append(f"\"{col_name}\" ILIKE :val_test")
                    conditions.append(f"\"{col_name}\" ILIKE :val_mock")
                # Integer columns (like version)
                elif data_type in ("integer", "bigint", "smallint"):
                    conditions.append(f"\"{col_name}\" = 999")
            
            params["val_test"] = "%test%"
            params["val_mock"] = "%mock%"
            
            if conditions:
                query_str = f"SELECT * FROM \"{table}\" WHERE " + " OR ".join(conditions)
                res = await conn.execute(text(query_str), params)
                rows = res.fetchall()
                if rows:
                    print(f"  Found {len(rows)} matching rows in table '{table}':")
                    for row in rows:
                        # Print row summary
                        row_dict = dict(row._mapping)
                        # Truncate large texts/jsons for display
                        display_dict = {}
                        for k, v in row_dict.items():
                            if isinstance(v, str) and len(v) > 100:
                                display_dict[k] = v[:100] + "..."
                            elif isinstance(v, dict):
                                display_dict[k] = str(v)[:100] + "..."
                            else:
                                display_dict[k] = v
                        print(f"    - {display_dict}")
                else:
                    print(f"  ✓ No leftover test/mock/999 data found in '{table}'.")
            else:
                print(f"  No relevant columns to check in '{table}'.")
                
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(check_cleanliness())
