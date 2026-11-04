"""Script-writing LLM providers (Gemini and Claude) behind one call."""

import logging
import os

import anthropic
from google import genai
from google.genai import types as genai_types

logger = logging.getLogger("radionow.llm")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-argon")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

PROVIDERS = {
    "gemini": f"Gemini ({GEMINI_MODEL})",
    "claude": f"Claude ({CLAUDE_MODEL})",
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "script": {"type": "string"},
    },
    "required": ["title", "summary", "script"],
    "additionalProperties": False,
}

_gemini_client: genai.Client | None = None
_claude_client: anthropic.AsyncAnthropic | None = None


def available_providers() -> dict[str, str]:
    """Providers whose API key is configured, mapped to a display label."""
    out = {}
    if GEMINI_API_KEY:
        out["gemini"] = PROVIDERS["gemini"]
    if ANTHROPIC_API_KEY:
        out["claude"] = PROVIDERS["claude"]
    return out


def default_provider() -> str:
    configured = os.getenv("DEFAULT_LLM", "").strip().lower()
    available = available_providers()
    if configured in available:
        return configured
    if available:
        return next(iter(available))
    return "gemini"


def resolve_provider(provider: str | None) -> str:
    provider = (provider or "").strip().lower()
    if provider in available_providers():
        return provider
    return default_provider()


async def generate_script_json(
    prompt: str, provider: str | None = None
) -> tuple[str, dict]:
    """Send a script prompt; return (raw JSON text, usage dict).

    usage = {"provider", "model", "input_tokens", "output_tokens"}.
    """
    provider = resolve_provider(provider)
    logger.info("Generating script with %s", PROVIDERS[provider])
    if provider == "claude":
        return await _claude_generate(prompt)
    return await _gemini_generate(prompt)


async def _gemini_generate(prompt: str) -> tuple[str, dict]:
    global _gemini_client
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    response = await _gemini_client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    text = response.text or ""
    if not text.strip():
        raise ValueError("Empty response from Gemini")
    meta = response.usage_metadata
    usage = {
        "provider": "gemini",
        "model": GEMINI_MODEL,
        "input_tokens": (meta and meta.prompt_token_count) or 0,
        # Thinking tokens are billed at the output rate.
        "output_tokens": ((meta and meta.candidates_token_count) or 0)
        + ((meta and meta.thoughts_token_count) or 0),
    }
    return text, usage


async def _claude_generate(prompt: str) -> tuple[str, dict]:
    global _claude_client
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    if _claude_client is None:
        _claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Streaming keeps long creative generations clear of HTTP timeouts.
    # Server-side fallback re-runs a declined request on another model.
    async with _claude_client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": SCRIPT_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = await stream.get_final_message()

    if message.stop_reason == "refusal":
        raise ValueError("Claude declined to write this script")
    if message.stop_reason == "max_tokens":
        raise ValueError("Claude hit the output limit before finishing the script")

    text = "".join(block.text for block in message.content if block.type == "text")
    if not text.strip():
        raise ValueError("Empty response from Claude")
    u = message.usage
    usage = {
        "provider": "claude",
        # The serving model (differs from CLAUDE_MODEL if a fallback ran).
        "model": message.model or CLAUDE_MODEL,
        "input_tokens": (u.input_tokens or 0)
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        + (getattr(u, "cache_read_input_tokens", 0) or 0),
        "output_tokens": u.output_tokens or 0,
    }
    return text, usage
