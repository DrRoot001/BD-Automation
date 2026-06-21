import asyncio
from module3.utils.storage import upload_file_to_supabase

async def test():
    with open("dummy.pdf", "wb") as f:
        f.write(b"dummy pdf content")
    url = await upload_file_to_supabase("dummy.pdf", "resume", "dummy_test_123.pdf")
    print(f"Result URL: {url}")

asyncio.run(test())
