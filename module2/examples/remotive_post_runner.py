"""Fetch Remotive RSS, normalize and post to backend."""
from __future__ import annotations

import asyncio
import httpx
from module2.adapters.rss_adapter import RssAdapter
from module2.normalization import normalize_batch


async def main():
    adapter = RssAdapter()
    rss = 'https://remotive.com/remote-jobs/rss'
    print('Fetching', rss)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36'
    }
    jobs = await adapter.discover_jobs({'rss_url': rss, 'request_headers': headers, 'retries': 4})
    print('got', len(jobs))
    if not jobs:
        return
    norm = normalize_batch(jobs)
    print('normalized', len(norm))
    payload = [ {k:v for k,v in job.to_dict().items() if k in {'title','company','location','source','source_url','canonical_url','description','skills','salary_min','salary_max','pay_period','job_type','posted_at','embedding'}} for job in norm[:5] ]
    print('posting', len(payload))
    try:
        resp = httpx.post('http://localhost:8000/api/jobs', json=payload, timeout=30.0)
        print('status', resp.status_code)
        print(resp.text)
    except Exception as e:
        print('post error', e)

if __name__ == '__main__':
    asyncio.run(main())
