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

class GeminiResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

class GroqResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

def _clean_response_text(text: str, is_json: bool) -> str:
    text = text.strip()
    if is_json:
        if "```" in text:
            start_idx = text.find("```")
            eol = text.find("\n", start_idx)
            if eol != -1:
                start_idx = eol
            else:
                start_idx += 3
            end_idx = text.rfind("```")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                text = text[start_idx:end_idx]
        
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]
        else:
            first_bracket = text.find("[")
            last_bracket = text.rfind("]")
            if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
                text = text[first_bracket:last_bracket + 1]
    return text.strip()

async def _generate_with_openrouter(api_key, contents, response_schema, temperature, max_retries, initial_delay, response_mime_type, system_instruction=None):
    openrouter_model = "anthropic/claude-3-haiku"

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

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": openrouter_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1500,
    }

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
            
            if resp.status_code == 429 or resp.status_code == 402:
                raise RuntimeError(f"Rate limit or quota hit ({resp.status_code}): {resp.text}")

            if resp.status_code != 200:
                raise ValueError(f"OpenRouter API error ({resp.status_code}): {resp.text}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise ValueError(f"OpenRouter returned empty choices: {data}")

            text_content = choices[0]["message"]["content"]
            is_json = bool(response_schema or response_mime_type == "application/json")
            text_content = _clean_response_text(text_content, is_json)

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            latency_ms = (time.time() - start_time) * 1000
            cost_usd = (prompt_tokens / 1_000_000 * 0.075) + (completion_tokens / 1_000_000 * 0.3)

            print(f"[OPENROUTER SUCCESS] Model: {openrouter_model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
            return OpenRouterResponse(text_content, prompt_tokens, completion_tokens)

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str or "402" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[OPENROUTER RETRY] Rate limit hit. Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[OPENROUTER ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

async def _generate_with_gemini(api_key, contents, response_schema, temperature, max_retries, initial_delay, model, response_mime_type, system_instruction=None):
    # Bound every Gemini call. The genai SDK's generate_content is a blocking
    # call with NO default timeout, so a single stalled response (which we hit
    # mid-pipeline) blocks the whole run indefinitely. http_options.timeout is
    # in milliseconds.
    try:
        gemini_timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", "90000"))
    except ValueError:
        gemini_timeout_ms = 90000
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=gemini_timeout_ms),
    )

    config_args = {}
    if response_schema:
        config_args["response_schema"] = response_schema
        config_args["response_mime_type"] = "application/json"
    elif response_mime_type:
        config_args["response_mime_type"] = response_mime_type

    if system_instruction:
        config_args["system_instruction"] = system_instruction

    config = types.GenerateContentConfig(**config_args)

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()
            start_time = time.time()
            # asyncio backstop: even if the SDK's own timeout fails to fire,
            # never let the event loop block longer than the configured budget
            # (+ slack). On timeout this raises and the retry/fallback handles it.
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=model,
                        contents=contents,
                        config=config
                    )
                ),
                timeout=(gemini_timeout_ms / 1000.0) + 15.0,
            )
            latency_ms = (time.time() - start_time) * 1000
            
            tokens_in = 0
            tokens_out = 0
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                tokens_in = getattr(response.usage_metadata, 'prompt_token_count', 0)
                tokens_out = getattr(response.usage_metadata, 'candidates_token_count', 0)
                
            cost_usd = (tokens_in / 1_000_000 * 0.10) + (tokens_out / 1_000_000 * 0.40)
            print(f"[GEMINI SUCCESS] Model: {model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {tokens_in}/{tokens_out} | Est. Cost: ${cost_usd:.6f}")
            is_json = bool(response_schema or response_mime_type == "application/json")
            cleaned_text = _clean_response_text(response.text, is_json)
            return GeminiResponse(cleaned_text, tokens_in, tokens_out)
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
    return None

async def _generate_with_groq(api_key, contents, response_schema, temperature, max_retries, initial_delay, response_mime_type, system_instruction=None):
    groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

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

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1500,
    }

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
    }

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload
                )
            
            if resp.status_code == 429 or resp.status_code == 402:
                raise RuntimeError(f"Rate limit or quota hit ({resp.status_code}): {resp.text}")

            if resp.status_code != 200:
                raise ValueError(f"Groq API error ({resp.status_code}): {resp.text}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise ValueError(f"Groq returned empty choices: {data}")

            text_content = choices[0]["message"]["content"]
            is_json = bool(response_schema or response_mime_type == "application/json")
            text_content = _clean_response_text(text_content, is_json)

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            latency_ms = (time.time() - start_time) * 1000
            cost_usd = 0.0

            print(f"[GROQ SUCCESS] Model: {groq_model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
            return GroqResponse(text_content, prompt_tokens, completion_tokens)

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str or "402" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[GROQ RETRY] Rate limit hit. Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[GROQ ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

class AnthropicResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

async def _generate_with_anthropic(api_key, contents, response_schema, temperature, max_retries, initial_delay, response_mime_type, system_instruction=None):
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

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
        "model": model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": user_content}],
    }
    if temperature is not None:
        payload["temperature"] = temperature

    system_prompt = system_instruction or ""
    if response_schema or response_mime_type == "application/json":
        json_prompt = "You are a strict JSON assistant. You must respond with valid JSON and nothing else."
        if response_schema:
            if hasattr(response_schema, "model_json_schema"):
                schema_desc = json.dumps(response_schema.model_json_schema(), indent=2)
            else:
                schema_desc = str(response_schema)
            json_prompt += f"\nReturn a valid JSON object matching this JSON Schema:\n{schema_desc}"
        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{json_prompt}"
        else:
            system_prompt = json_prompt

    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload
                )
            
            if resp.status_code == 429 or resp.status_code == 402:
                raise RuntimeError(f"Rate limit or quota hit ({resp.status_code}): {resp.text}")

            if resp.status_code != 200:
                raise ValueError(f"Anthropic API error ({resp.status_code}): {resp.text}")

            data = resp.json()
            content_list = data.get("content", [])
            if not content_list:
                raise ValueError(f"Anthropic returned empty content: {data}")

            text_content = "".join([c.get("text", "") for c in content_list if c.get("type") == "text"]).strip()
            
            usage = data.get("usage", {})
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)

            latency_ms = (time.time() - start_time) * 1000
            cost_usd = (prompt_tokens / 1_000_000 * 3.0) + (completion_tokens / 1_000_000 * 15.0)

            print(f"[ANTHROPIC SUCCESS] Model: {model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
            is_json = bool(response_schema or response_mime_type == "application/json")
            text_content = _clean_response_text(text_content, is_json)
            return AnthropicResponse(text_content, prompt_tokens, completion_tokens)

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str or "402" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[ANTHROPIC RETRY] Rate limit hit. Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[ANTHROPIC ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

# Keep track of the working provider key across calls to avoid repeatedly switching/retrying failed providers
_working_provider_key = None

async def generate_content_with_retry(
    contents: Union[str, List[Any]],
    response_schema: Any = None,
    temperature: float = 0.3,
    max_retries: int = 10,
    initial_delay: float = 5.0,
    model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    response_mime_type: str = None,
    system_instruction: str = None
) -> Any:
    """
    Wrap model generation with retry, trying available providers in fallback sequence.
    """
    global _working_provider_key
    providers = []
    seen_keys = set()

    # Detect multimodal requests (a list containing non-text items, e.g. PIL images).
    # Only the Gemini path forwards images to a vision model; the Groq/Anthropic/
    # OpenRouter paths flatten contents with str(item), which silently destroys
    # images and yields an empty/all-null parse. Route these to Gemini's vision model.
    is_multimodal = isinstance(contents, list) and any(
        not isinstance(c, str) and not hasattr(c, "text") for c in contents
    )
    if is_multimodal:
        model = os.getenv("GEMINI_VISION_MODEL", model)

    # Provider priority: GEMINI FIRST. Operator topped up the Gemini account and
    # wants it to be the primary path — Groq's daily-TPD ceiling (100k tokens)
    # and Anthropic's per-credit billing made them poor primaries. Gemini is the
    # vision-capable provider AND the operator's preferred LLM, so it always
    # leads. Groq stays second as a cheap fast fallback when it has quota;
    # Anthropic last so a paid credit isn't burned on a transient Gemini blip.
    candidates = [
        ("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
        ("GROQ_API_KEY", os.getenv("GROQ_API_KEY")),
        ("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY")),
        ("ANTHROPIC_API_KEY_2", os.getenv("ANTHROPIC_API_KEY_2")),
        ("CLAUDE_API_KEY", os.getenv("CLAUDE_API_KEY")),
        ("OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY")),
    ]
    
    for name, key in candidates:
        if not key:
            continue
        key = key.strip()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        
        if key.startswith("gsk_"):
            providers.append(("groq", key))
        elif key.startswith("sk-or-"):
            providers.append(("openrouter", key))
        elif key.startswith("sk-ant-") or key.startswith("sk-"):
            providers.append(("anthropic", key))
        else:
            providers.append(("gemini", key))

    if is_multimodal:
        vision_providers = [p for p in providers if p[0] == "gemini"]
        if not vision_providers:
            raise ValueError(
                "Multimodal request (image-based PDF) requires a vision-capable "
                "provider, but no Gemini API key (GEMINI_API_KEY) is configured. "
                "The Groq/Anthropic/OpenRouter text paths cannot process images."
            )
        providers = vision_providers

    if not providers:
        raise ValueError("No AI API keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, etc.) configured in environment variables.")

    # Reorder providers to place the last known working API key at the front of the list
    if _working_provider_key:
        working_idx = -1
        for idx, (p_type, api_key) in enumerate(providers):
            if api_key == _working_provider_key:
                working_idx = idx
                break
        if working_idx > 0:
            working_provider = providers.pop(working_idx)
            providers.insert(0, working_provider)
            print(f"[API ROUTER] Starting with last known working provider: {working_provider[0]}")

    last_error = None
    
    for idx, (provider_type, api_key) in enumerate(providers):
        # If there are subsequent providers available, retry at most once before falling back
        has_fallback = idx < len(providers) - 1
        current_max_retries = 2 if has_fallback else max_retries
        
        try:
            if provider_type == "groq":
                res = await _generate_with_groq(
                    api_key, contents, response_schema, temperature, 
                    current_max_retries, initial_delay, response_mime_type,
                    system_instruction=system_instruction
                )
                _working_provider_key = api_key
                return res
            elif provider_type == "openrouter":
                res = await _generate_with_openrouter(
                    api_key, contents, response_schema, temperature, 
                    current_max_retries, initial_delay, response_mime_type,
                    system_instruction=system_instruction
                )
                _working_provider_key = api_key
                return res
            elif provider_type == "gemini":
                res = await _generate_with_gemini(
                    api_key, contents, response_schema, temperature, 
                    current_max_retries, initial_delay, model, response_mime_type,
                    system_instruction=system_instruction
                )
                _working_provider_key = api_key
                return res
            elif provider_type == "anthropic":
                res = await _generate_with_anthropic(
                    api_key, contents, response_schema, temperature,
                    current_max_retries, initial_delay, response_mime_type,
                    system_instruction=system_instruction
                )
                _working_provider_key = api_key
                return res
        except Exception as e:
            last_error = e
            print(f"[FALLBACK] Provider '{provider_type}' failed with error: {e}")
            # If the current working key failed, clear it so we don't assume it works next time
            if api_key == _working_provider_key:
                _working_provider_key = None
            # DNS / network failures hit every provider equally — there's no
            # point burning a paid Anthropic credit because Gemini's DNS
            # resolved slow for 3 seconds. Re-raise immediately so the caller
            # (matching / orchestrator) retries the whole step on its own
            # schedule, instead of cascading down a fallback chain that will
            # also fail the same way.
            err_str = str(e).lower()
            _transient_network = (
                "getaddrinfo" in err_str
                or "winerror 10060" in err_str
                or "name or service not known" in err_str
                or "temporary failure in name resolution" in err_str
                or "connection attempt failed" in err_str
            )
            if _transient_network:
                print(f"[FALLBACK] Network/DNS error on {provider_type} — NOT cascading to paid fallback; re-raise so caller can retry the whole step.")
                raise
            if has_fallback:
                print(f"-> Trying next available API key...")

    print("[ERROR] All available API providers failed.")
    raise last_error


