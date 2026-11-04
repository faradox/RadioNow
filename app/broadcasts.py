"""Shared broadcast pipeline: script -> speech -> stored broadcast document."""

import asyncio
import logging
from datetime import datetime

from app.database import get_broadcasts_collection, get_voices_collection
from app.generator import (
    generate_script,
    get_audio_duration,
    get_voice_audio_path,
    new_broadcast_audio_path,
)
from app.llm import resolve_provider
from app.tts import clone_minimax_voice, render_speech, resolve_engine

logger = logging.getLogger("radionow.broadcasts")

# Serialize TTS renders so the daily job and admin requests don't compete.
_render_lock = asyncio.Lock()


def engine_for_voice(voice: dict, override: str | None = None) -> str:
    return resolve_engine(override or voice.get("tts_engine"))


async def ensure_minimax_voice_id(voice: dict, force_new: bool = False) -> str:
    """Return the voice's MiniMax voice id, cloning (and storing) it if needed."""
    if voice.get("minimax_voice_id") and not force_new:
        return voice["minimax_voice_id"]
    audio_path = get_voice_audio_path(voice["audio_filename"])
    voice_id = await asyncio.to_thread(clone_minimax_voice, audio_path)
    await get_voices_collection().update_one(
        {"_id": voice["_id"]},
        {"$set": {"minimax_voice_id": voice_id, "minimax_cloned_at": datetime.utcnow()}},
    )
    voice["minimax_voice_id"] = voice_id
    return voice_id


async def render_for_voice(
    voice: dict,
    script: str,
    engine: str,
    style_instruction: str | None,
    chunk_minutes: float | None,
):
    """Render the script to an MP3 and return (path, duration_seconds)."""
    output_path = new_broadcast_audio_path()
    kwargs = dict(
        voice_audio_path=get_voice_audio_path(voice["audio_filename"]),
        reference_text=voice["reference_text"],
        style_instruction=style_instruction,
        chunk_minutes=chunk_minutes,
    )
    try:
        async with _render_lock:
            if engine == "minimax":
                kwargs["minimax_voice_id"] = await ensure_minimax_voice_id(voice)
                try:
                    await asyncio.to_thread(
                        render_speech, script, output_path, engine, **kwargs
                    )
                except Exception as e:
                    # Cloned voices can expire upstream; re-clone once and retry.
                    logger.warning("MiniMax render failed (%s); re-cloning voice", e)
                    kwargs["minimax_voice_id"] = await ensure_minimax_voice_id(
                        voice, force_new=True
                    )
                    await asyncio.to_thread(
                        render_speech, script, output_path, engine, **kwargs
                    )
            else:
                await asyncio.to_thread(
                    render_speech, script, output_path, engine, **kwargs
                )
    except Exception:
        if output_path.exists():
            output_path.unlink()
        raise
    return output_path, get_audio_duration(output_path)


async def create_broadcast(
    voice: dict,
    prompt: str,
    *,
    input_text: str,
    llm_provider: str | None = None,
    tts_engine: str | None = None,
    style_override: str | None = None,
    chunk_minutes: float | None = 1.0,
    extra_fields: dict | None = None,
) -> dict:
    """Generate script and audio, store the broadcast, return the document."""
    provider = resolve_provider(llm_provider)
    engine = engine_for_voice(voice, tts_engine)
    style_instruction = style_override or voice.get("style_instruction")

    title, script, summary = await generate_script(prompt, provider)
    audio_path, duration_seconds = await render_for_voice(
        voice, script, engine, style_instruction, chunk_minutes
    )

    topic = input_text[:50].strip() + ("..." if len(input_text) > 50 else "")
    doc = {
        "title": title,
        "voice_id": str(voice["_id"]),
        "topic": topic,
        "input_text": input_text,
        "generated_script": script,
        "summary": summary,
        "audio_filename": audio_path.name,
        "duration_seconds": duration_seconds,
        "style_override": style_override,
        "llm_provider": provider,
        "tts_engine": engine,
        "generate_in_chunks": chunk_minutes is not None,
        "chunk_minutes": chunk_minutes,
        "kind": "manual",
        "created_at": datetime.utcnow(),
        **(extra_fields or {}),
    }
    result = await get_broadcasts_collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    logger.info("Created broadcast: %s (%s, %s/%s)", title, doc["_id"], provider, engine)
    return doc
