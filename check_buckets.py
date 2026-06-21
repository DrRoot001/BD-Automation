import asyncio
import asyncpg

async def run():
    conn = await asyncpg.connect("postgresql://postgres.rdfnydteruigmajebxsm:JOWA1pMxYz70t1YR@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres")
    rows = await conn.fetch("SELECT id, name, public FROM storage.buckets;")
    for row in rows:
        print(dict(row))
    await conn.close()

asyncio.run(run())
