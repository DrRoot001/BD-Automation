import os
import json
import asyncio
import time
import httpx
from typing import Any, List, Union
from google import genai
from google.genai import types

class OpenRouterResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

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
    Wrap client.models.generate_content with retry, routing to OpenRouter if an OpenRouter API key is detected.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")

    # Check if this is an OpenRouter API key
    if api_key.startswith("sk-or-"):
        # Map model name to Anthropic model as requested
        openrouter_model = "anthropic/claude-3-haiku"

        # Construct message payload
        if isinstance(contents, list):
            user_content = ""
            for item in contents:
                if isinstance(item, str):
                    user_content += item
                elif hasattr(item, "text"):
                    user_content += item.text
                else:
                    user_content += str(item)
        else:
            user_content = str(contents)

        payload = {
            "model": openrouter_model,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "max_tokens": 4000,
        }

        # Handle JSON schema constraint
        if response_schema or response_mime_type == "application/json":
            payload["response_format"] = {"type": "json_object"}
            if response_schema:
                if hasattr(response_schema, "model_json_schema"):
                    schema_desc = json.dumps(response_schema.model_json_schema(), indent=2)
                else:
                    schema_desc = str(response_schema)
                payload["messages"][0]["content"] += f"\n\nCRITICAL: You must return valid JSON that conforms strictly to this JSON Schema:\n{schema_desc}"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/sabihhaider/BD-Automator-Agent",
            "X-Title": "BD Automator Agent",
        }

        delay = initial_delay
        for attempt in range(max_retries):
            try:
                start_time = time.time()
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json=payload
                    )
                
                if resp.status_code == 429:
                    raise RuntimeError("Rate limit hit")

                if resp.status_code != 200:
                    raise ValueError(f"OpenRouter API error ({resp.status_code}): {resp.text}")

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise ValueError(f"OpenRouter returned empty choices: {data}")

                text_content = choices[0]["message"]["content"]
                if response_schema or response_mime_type == "application/json":
                    text_content = text_content.strip()
                    if "```" in text_content:
                        start_idx = text_content.find("```")
                        eol = text_content.find("\n", start_idx)
                        if eol != -1:
                            start_idx = eol
                        end_idx = text_content.rfind("```")
                        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                            text_content = text_content[start_idx:end_idx]
                    first_brace = text_content.find("{")
                    last_brace = text_content.rfind("}")
                    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                        text_content = text_content[first_brace:last_brace + 1]
                    text_content = text_content.strip()

                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)

                latency_ms = (time.time() - start_time) * 1000
                cost_usd = (prompt_tokens / 1_000_000 * 0.075) + (completion_tokens / 1_000_000 * 0.3)

                print(f"[OPENROUTER SUCCESS] Model: {openrouter_model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
                return OpenRouterResponse(text_content, prompt_tokens, completion_tokens)

            except Exception as e:
                err_str = str(e)
                is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str

                if is_rate_limit and attempt < max_retries - 1:
                    print(f"[OPENROUTER RETRY] Rate limit hit (429). Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                    await asyncio.sleep(delay)
                    delay = min(30.0, delay * 2.0)
                else:
                    print(f"[OPENROUTER ERROR] Attempt {attempt+1} failed: {e}")
                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(1.0)
        return

    # Fallback to standard Google GenAI SDK
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
                
            # Rough cost estimate
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

