import asyncio
from module3.utils.storage import upload_file_to_supabase

async def test():
    file_path = "/Users/sabihhaider/Documents/BD-Automator-Agent/Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"
    url = await upload_file_to_supabase(file_path, "resume", "Sabih_Haider_Base_Resume.pdf")
    print(f"Result URL: {url}")

asyncio.run(test())
