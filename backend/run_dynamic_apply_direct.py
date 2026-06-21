import asyncio
from app.tasks.dynamic_apply import _run

if __name__ == "__main__":
    cand_id = "28052ebd-5e0c-41cb-a803-23e315ff56fb"
    print(f"Running dynamic_apply directly for {cand_id}...")
    res = asyncio.run(_run(cand_id, 1))
    print(f"Result: {res}")
