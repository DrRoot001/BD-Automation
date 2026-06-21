import asyncio
import asyncpg

async def run():
    conn = await asyncpg.connect("postgresql://postgres.rdfnydteruigmajebxsm:JOWA1pMxYz70t1YR@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres")
    await conn.execute("UPDATE storage.buckets SET public = true WHERE name IN ('resume', 'cover_letter', 'updated_resume', 'screenshots');")
    print("Buckets updated to public: True")
    await conn.close()

asyncio.run(run())
