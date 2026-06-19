import os
import time
import asyncio
import logging
import httpx
from typing import Literal, Optional
from playwright.async_api import Page
from dotenv import load_dotenv
from .models import CaptchaSolution

load_dotenv()

logger = logging.getLogger(__name__)

class CaptchaService:
    def __init__(self, api_key: Optional[str] = None, provider: str = "2captcha"):
        self.provider = provider.lower()
        if self.provider not in ["2captcha", "anticaptcha"]:
            raise ValueError("Provider must be '2captcha' or 'anticaptcha'")
        
        if not api_key:
            if self.provider == "2captcha":
                self.api_key = os.getenv("TWO_CAPTCHA_API_KEY")
            else:
                self.api_key = os.getenv("ANTI_CAPTCHA_API_KEY")
        else:
            self.api_key = api_key

        if not self.api_key:
            raise ValueError(f"API Key for {self.provider} not found in environment")

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> CaptchaSolution:
        start_time = time.time()
        token = None
        cost = 0.002 # Default estimated cost

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "2captcha":
                    resp = await client.post(
                        "https://2captcha.com/in.php",
                        data={
                            "key": self.api_key,
                            "method": "userrecaptcha",
                            "googlekey": site_key,
                            "pageurl": page_url,
                            "json": 1
                        }
                    )
                    data = resp.json()
                    if data.get("status") != 1:
                        raise Exception(f"2Captcha creation failed: {data.get('request')}")
                    
                    request_id = data.get("request")
                    
                    for _ in range(12): # 120 seconds max
                        await asyncio.sleep(10)
                        res_resp = await client.get(
                            f"https://2captcha.com/res.php?key={self.api_key}&action=get&id={request_id}&json=1"
                        )
                        res_data = res_resp.json()
                        if res_data.get("status") == 1:
                            token = res_data.get("request")
                            break
                        elif res_data.get("request") == "CAPCHA_NOT_READY":
                            continue
                        else:
                            raise Exception(f"2Captcha error: {res_data.get('request')}")

                elif self.provider == "anticaptcha":
                    resp = await client.post(
                        "https://api.anti-captcha.com/createTask",
                        json={
                            "clientKey": self.api_key,
                            "task": {
                                "type": "NoCaptchaTaskProxyless",
                                "websiteURL": page_url,
                                "websiteKey": site_key
                            }
                        }
                    )
                    data = resp.json()
                    if data.get("errorId") != 0:
                        raise Exception(f"AntiCaptcha creation failed: {data.get('errorDescription')}")
                    
                    task_id = data.get("taskId")
                    
                    for _ in range(12):
                        await asyncio.sleep(10)
                        res_resp = await client.post(
                            "https://api.anti-captcha.com/getTaskResult",
                            json={
                                "clientKey": self.api_key,
                                "taskId": task_id
                            }
                        )
                        res_data = res_resp.json()
                        if res_data.get("status") == "ready":
                            token = res_data.get("solution", {}).get("gRecaptchaResponse")
                            break
                        elif res_data.get("errorId") != 0:
                            raise Exception(f"AntiCaptcha error: {res_data.get('errorDescription')}")

            if token:
                solve_time = time.time() - start_time
                return CaptchaSolution(
                    captcha_type="recaptcha_v2",
                    token=token,
                    success=True,
                    solve_time_seconds=solve_time,
                    cost_usd=cost
                )
        except Exception as e:
            logger.error(f"Error solving Recaptcha V2: {e}")
            
        return CaptchaSolution(
            captcha_type="recaptcha_v2",
            success=False,
            solve_time_seconds=time.time() - start_time,
            cost_usd=0
        )

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> CaptchaSolution:
        start_time = time.time()
        token = None
        cost = 0.002

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "2captcha":
                    resp = await client.post(
                        "https://2captcha.com/in.php",
                        data={
                            "key": self.api_key,
                            "method": "hcaptcha",
                            "sitekey": site_key,
                            "pageurl": page_url,
                            "json": 1
                        }
                    )
                    data = resp.json()
                    if data.get("status") != 1:
                        raise Exception(f"2Captcha hcaptcha creation failed: {data.get('request')}")
                    
                    request_id = data.get("request")
                    for _ in range(12):
                        await asyncio.sleep(10)
                        res_resp = await client.get(
                            f"https://2captcha.com/res.php?key={self.api_key}&action=get&id={request_id}&json=1"
                        )
                        res_data = res_resp.json()
                        if res_data.get("status") == 1:
                            token = res_data.get("request")
                            break
                        elif res_data.get("request") == "CAPCHA_NOT_READY":
                            continue

                elif self.provider == "anticaptcha":
                    resp = await client.post(
                        "https://api.anti-captcha.com/createTask",
                        json={
                            "clientKey": self.api_key,
                            "task": {
                                "type": "HCaptchaTaskProxyless",
                                "websiteURL": page_url,
                                "websiteKey": site_key
                            }
                        }
                    )
                    data = resp.json()
                    if data.get("errorId") != 0:
                        raise Exception(f"AntiCaptcha hcaptcha creation failed: {data.get('errorDescription')}")
                    
                    task_id = data.get("taskId")
                    for _ in range(12):
                        await asyncio.sleep(10)
                        res_resp = await client.post(
                            "https://api.anti-captcha.com/getTaskResult",
                            json={"clientKey": self.api_key, "taskId": task_id}
                        )
                        res_data = res_resp.json()
                        if res_data.get("status") == "ready":
                            token = res_data.get("solution", {}).get("gRecaptchaResponse") # hCaptcha often uses this field name in anticaptcha too
                            break

            if token:
                solve_time = time.time() - start_time
                return CaptchaSolution(
                    captcha_type="hcaptcha",
                    token=token,
                    success=True,
                    solve_time_seconds=solve_time,
                    cost_usd=cost
                )
        except Exception as e:
            logger.error(f"Error solving HCaptcha: {e}")
            
        return CaptchaSolution(
            captcha_type="hcaptcha",
            success=False,
            solve_time_seconds=time.time() - start_time,
            cost_usd=0
        )

    async def solve(self, page: Page, captcha_type: str) -> CaptchaSolution:
        page_url = page.url
        site_key = None
        
        async def perform_solve():
            nonlocal site_key
            if captcha_type == "recaptcha_v2":
                el = await page.query_selector('.g-recaptcha')
                if el:
                    site_key = await el.get_attribute('data-sitekey')
                if not site_key:
                    # Try finding in iframe
                    iframe = await page.query_selector('iframe[src*="recaptcha/api2/anchor"]')
                    if iframe:
                        src = await iframe.get_attribute('src')
                        import urllib.parse
                        parsed = urllib.parse.urlparse(src)
                        site_key = urllib.parse.parse_qs(parsed.query).get('k', [None])[0]
                
                if site_key:
                    solution = await self.solve_recaptcha_v2(site_key, page_url)
                    if solution.success:
                        await page.evaluate(f"""
                            (token) => {{
                                const el = document.getElementById('g-recaptcha-response');
                                if (el) el.innerHTML = token;
                                // Trigger callback if present
                                const container = document.querySelector('.g-recaptcha');
                                if (container) {{
                                    const callback = container.getAttribute('data-callback');
                                    if (callback && window[callback]) window[callback](token);
                                }}
                            }}
                        """, solution.token)
                    return solution

            elif captcha_type == "hcaptcha":
                el = await page.query_selector('.h-captcha')
                if el:
                    site_key = await el.get_attribute('data-sitekey')
                
                if site_key:
                    solution = await self.solve_hcaptcha(site_key, page_url)
                    if solution.success:
                        await page.evaluate(f"""
                            (token) => {{
                                const el = document.querySelector('[name="h-captcha-response"]');
                                if (el) el.value = token;
                                const container = document.querySelector('.h-captcha');
                                if (container) {{
                                    const callback = container.getAttribute('data-callback');
                                    if (callback && window[callback]) window[callback](token);
                                }}
                            }}
                        """, solution.token)
                    return solution
            
            return CaptchaSolution(captcha_type=captcha_type, success=False, solve_time_seconds=0, cost_usd=0)

        solution = await perform_solve()
        if not solution.success:
            logger.info(f"Captcha solving failed for {captcha_type}, retrying in 5 seconds...")
            await asyncio.sleep(5)
            solution = await perform_solve()

        logger.info(f"Captcha solve attempt: type={captcha_type}, provider={self.provider}, "
                    f"success={solution.success}, time={solution.solve_time_seconds:.2f}s, cost=${solution.cost_usd}")
        
        return solution
