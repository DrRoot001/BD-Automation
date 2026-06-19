"""Gemini API client utility with retry and backoff logic to handle rate limiting."""
from __future__ import annotations

import os
import asyncio
import time
from typing import Any, List, Union
from google import genai
from google.genai import types

async def generate_content_with_retry(
    contents: Union[str, List[Any]],
    response_schema: Any = None,
    temperature: float = 0.3,
    max_retries: int = 10,
    initial_delay: float = 5.0,
    model: str = "gemini-2.5-flash",
    response_mime_type: str = None
) -> Any:
    """
    Wrap client.models.generate_content with retry on 429 / RESOURCE_EXHAUSTED errors.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")

    client = genai.Client(api_key=api_key)

    # Configure the content generation options
    config_args = {}
    if response_schema:
        config_args["response_schema"] = response_schema
        config_args["response_mime_type"] = "application/json"
    elif response_mime_type:
        config_args["response_mime_type"] = response_mime_type

    if temperature is not None:
        config_args["temperature"] = temperature

    config = types.GenerateContentConfig(**config_args)

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()
            start_time = time.time()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
            )
            latency_ms = (time.time() - start_time) * 1000
            
            tokens_in = 0
            tokens_out = 0
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                tokens_in = getattr(response.usage_metadata, 'prompt_token_count', 0)
                tokens_out = getattr(response.usage_metadata, 'candidates_token_count', 0)
                
            # Rough cost estimate for gemini-2.0-flash (as of early 2025: $0.10/1M input, $0.40/1M output for short prompts)
            cost_usd = (tokens_in / 1_000_000 * 0.10) + (tokens_out / 1_000_000 * 0.40)
            
            print(f"[GEMINI SUCCESS] Model: {model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {tokens_in}/{tokens_out} | Est. Cost: ${cost_usd:.6f}")
            return response
        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[GEMINI RETRY] Rate limit hit (429). Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[GEMINI ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
