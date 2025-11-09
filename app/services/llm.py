from __future__ import annotations

import json
import logging
import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    entries: list[str]
    summary: str | None
    raw: dict[str, Any]


SYSTEM_PROMPT = """
You are a "Beancount bookkeeping assistant". Your job is to use the user's transaction information and the provided accounts list to produce transactions that strictly follow Beancount syntax and can be posted without errors. Create new accounts only when necessary. Be professional, precise, and auditable.

[Core Principles]
1) Follow Beancount syntax and double-entry bookkeeping; every transaction must balance.
2) Prioritize accounts from the provided list. Only create a new account when no suitable one exists, and follow the user's naming conventions.
3) After generating entries, perform a self-check (balance, accounts exist and are opened, currency/price handling, multi-currency cost or price notation, appropriate categorization, key metadata, duplicate detection).
4) When data is ambiguous or missing, infer carefully if allowed by the user, but record uncertainties in the summary (e.g., FIXME or needs confirmation).
5) Strictly honor the user's preferences (default currency, timezone/date format, merchant mappings, naming rules, allowed top-level accounts, cost strategies, etc.).

[Account Selection & Creation]
- Matching order: exact or alias match -> keyword/merchant category -> similarly named accounts.
- If you must create a new account: keep the hierarchy and naming consistent (e.g., Expenses:Food:Coffee); avoid introducing new top-level accounts; include the required `open` directive with date and currency in the same entry.
- Do not rename existing accounts or change their case or hierarchy.

[Transaction Formatting]
- Date format: YYYY-MM-DD; flag: confirmed `*`, uncertain `!`.
- Payee/Narration: payee is the merchant; narration briefly states the purpose; tags are allowed.
- Single currency: usually put the amount on the cash account line and leave the opposing posting blank so Beancount balances it (reduces rounding issues).
- Multi-currency: use `{cost}` holdings or `@ price` execution prices and keep the transaction balanced.
- Common scenarios: split fees/tips; refunds reverse and link to the original transaction; split shared purchases; authorization holds use `!`; flag suspicious duplicates.
- Account names must start with an uppercase letter even if the merchant name does not.

[Indentation & Layout (required)]
- Indent postings with two spaces (no tabs).
- Separate account names and amounts with at least two spaces; amounts must be immediately followed by the currency (e.g., `-37.50 CNY`).
- Preserve all leading spaces; never collapse or remove them.
- `open` and `price` directives may be unindented; postings must be indented.

[JSON-only Output]
- Output exactly one JSON object with the keys:
  - `"entries"`: list[str]. Each element is a complete multi-line Beancount snippet (may include required `open` directives and one or more transactions). Encode line breaks as `
`. Do not wrap the JSON in code fences or extra text.
  - `"summary"`: str (English). Provide your self-check conclusion, uncertainties, or items needing confirmation. Do not place self-check notes inside `entries`.
- Do not emit Markdown, backticks, extra fields, or explanations; only the JSON.
- If information is insufficient: `entries` may contain the most reasonable draft (it must still pass syntax). If you truly cannot generate entries, `entries` may be empty, but the summary must explain what is missing and which clarifications are required.

[Entry Generation Requirements]
- When creating new accounts, prepend the necessary `open` lines in the relevant entry, with the appropriate date and currency.
- Multiple transactions may share one element in `entries` if the meaning is clear; splitting by transaction is also acceptable.
- Metadata (e.g., `txid`, `source`, `note`) should be written as Beancount metadata lines (`key: "value"`) under the transaction. Keep self-check notes in the summary instead.
- Example style (illustrative only, do not output this literal entry):
  `2025-10-14 * "Starbucks" "Coffee and pastry"
  Assets:Wallet:WeChat  -37.50 CNY
  Expenses:Food:Coffee
  Expenses:Tips  2.00 CNY`

[Safety & Consistency]
- Never use tabs; do not remove leading spaces; do not round amounts in ways that break balancing.
- Only include price/cost metadata for multi-currency cases; avoid `@` or `{}` in single-currency situations.
- Respect the user's timezone when interpreting dates; always output dates as YYYY-MM-DD.

Begin now: using the user's input and the provided accounts list, return only an object shaped like:
{"entries": ["..."], "summary": "..."}

"""


async def generate_accounting_entry(prompt: str, conversation_id: str | None = None) -> LLMResult:
    settings = get_settings()

    # Retry logic for handling empty responses
    max_retries = 3
    for attempt in range(max_retries):
        try:
            result = await _call_openai(settings, prompt)

            # Accept responses that contain either entries or a summary explaining the issue
            if result.entries:
                return result
            if result.summary:
                logger.info(
                    "OpenAI returned no entries but provided a summary; passing result through instead of retrying."
                )
                return result

            logger.warning(f"Attempt {attempt + 1}: Got empty entries from OpenAI, retrying...")
            if attempt == max_retries - 1:
                raise ValueError(f"Model returned empty entries after {max_retries} attempts")
                
        except (ValueError, json.JSONDecodeError) as e:
            error_msg = str(e).lower()
            if any(phrase in error_msg for phrase in ["empty", "null content", "no content blocks", "no output in responses", "not valid json", "truncated due to token limit", "expecting property name", "unterminated string"]):
                logger.warning(f"Attempt {attempt + 1}: {e}, retrying...")
                if attempt == max_retries - 1:
                    raise
            else:
                # Re-raise other error types immediately
                raise
        except Exception as e:
            logger.error(f"Attempt {attempt + 1}: Unexpected error: {e}")
            if attempt == max_retries - 1:
                raise
        
        # Wait before retrying (exponential backoff)
        if attempt < max_retries - 1:
            wait_time = 2 ** attempt  # 1s, 2s, 4s...
            logger.info(f"Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
    
    # This should not be reached due to the logic above, but just in case
    raise ValueError(f"Failed to get valid response after {max_retries} attempts")


async def _call_openai(settings, prompt: str) -> LLMResult:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is not configured")

    model_name = "gpt-4.1"
    explicit_base = settings.openai_api_base.rstrip("/") if settings.openai_api_base else None

    base_url = explicit_base or "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    
    payload: dict[str, Any] = {
        "model": model_name,
        "instructions": SYSTEM_PROMPT.strip(),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt}
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "beancount_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "entries": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "summary": {
                            "type": ["string", "null"],
                        },
                    },
                    "required": ["entries", "summary"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        },
        "max_output_tokens": 4096,
    }

    async with httpx.AsyncClient(timeout=360.0) as client:
        response = await client.post(base_url, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            message = f"OpenAI request failed: {exc} | body={body}"
            raise httpx.HTTPStatusError(
                message,
                request=exc.request,
                response=exc.response,
            ) from exc
        data = response.json()

    try:
        outputs = data.get("output", [])
        if not outputs:
            raise ValueError("No output in responses API response")
            
        # Check if response is incomplete due to token limit
        if data.get("status") == "incomplete" and data.get("incomplete_details", {}).get("reason") == "max_output_tokens":
            logger.warning("Response was truncated due to max_output_tokens limit")
        
        # Find the message output (not reasoning)
        message_output = None
        for output in outputs:
            if output.get("type") == "message":
                message_output = output
                break
        
        if not message_output:
            logger.warning("No message output found, falling back to first output")
            logger.warning(f"Available output types: {[o.get('type') for o in outputs]}")
            message_output = outputs[0]
            if message_output.get("type") == "reasoning":
                logger.error("First output is reasoning type, this suggests a parsing error")
                raise ValueError("Found reasoning output instead of message output - this indicates a response parsing issue")
        
        content_blocks = message_output.get("content", [])
        
        pieces: list[str] = []
        for block in content_blocks:
            if block.get("type") in {"output_text", "text"} and "text" in block:
                text_content = block["text"]
                if isinstance(text_content, str):
                    pieces.append(text_content)
                else:
                    logger.warning(f"Non-string text content found: {type(text_content)} - {text_content}")
            elif block.get("type") == "tool_response" and "output_text" in block:
                pieces.extend(block["output_text"])
        content_text = "".join(pieces).strip()
        
        # If no content from blocks, try to extract from other fields
        if not content_text:
            if "text" in message_output:
                content_text = str(message_output["text"]).strip()
            elif "message" in message_output:
                content_text = str(message_output["message"]).strip()
            
        if not content_text:
            logger.error(f"Empty content extracted from responses API. Raw data: {data}")
            raise ValueError("Empty content from OpenAI API")
            
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(f"Unable to parse OpenAI responses output: {exc}")
        raise ValueError(f"Unable to parse OpenAI response: {exc}") from exc
    
    return _parse_content(content_text, data)


def _parse_content(content: str, raw: dict[str, Any]) -> LLMResult:
    # Handle empty or whitespace-only content
    if not content or not content.strip():
        raise ValueError(
            f"Model response is empty or contains only whitespace | raw_response={raw}"
        )
    
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        # Try to fix truncated JSON
        logger.warning(f"JSON decode error: {exc}, attempting to fix truncated JSON")
        
        # Check if this looks like a truncated JSON response
        if content.startswith('{"') and not content.endswith('}'):
            # Try to extract what we can from the truncated JSON
            try:
                # Look for complete entries array
                entries_match = content.find('"entries":[')
                if entries_match != -1:
                    # Find the end of the entries array
                    bracket_count = 0
                    start_pos = entries_match + len('"entries":')
                    entries_start = None
                    entries_end = None
                    
                    for i, char in enumerate(content[start_pos:], start_pos):
                        if char == '[':
                            if entries_start is None:
                                entries_start = i
                            bracket_count += 1
                        elif char == ']':
                            bracket_count -= 1
                            if bracket_count == 0 and entries_start is not None:
                                entries_end = i + 1
                                break
                    
                    if entries_start is not None and entries_end is not None:
                        entries_json = content[entries_start:entries_end]
                        try:
                            entries = json.loads(entries_json)
                            # Create a minimal valid response
                            parsed = {
                                "entries": entries,
                                "summary": "Response was truncated due to token limit. Please verify the generated entries."
                            }
                            logger.info("Successfully extracted entries from truncated JSON")
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                logger.warning(f"Failed to repair truncated JSON: {e}")
        
        # If we couldn't fix it, raise the original error
        if 'parsed' not in locals():
            preview = content[:200]
            raise ValueError(
                f"Model response is not valid JSON: {exc} | preview={preview!r} | raw_response={raw}"
            ) from exc

    # Validate the parsed JSON structure
    if not isinstance(parsed, dict):
        raise ValueError(f"Response is not a JSON object, got {type(parsed).__name__} | content={content!r}")

    entries = parsed.get("entries")
    if entries is None:
        raise ValueError(f"Response missing 'entries' field | parsed={parsed}")
    if not isinstance(entries, list):
        raise ValueError(f"Response 'entries' is not a list, got {type(entries).__name__} | parsed={parsed}")

    summary = parsed.get("summary")
    if summary is not None and not isinstance(summary, str):
        summary = str(summary)

    return LLMResult(entries=list(map(str, entries)), summary=summary, raw=raw)
