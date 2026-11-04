"""Shared broadcast pipeline: script -> speech -> stored broadcast document."""

import asyncio
import logging
from datetime import datetime

from app.costs import CostTracker
from app.database import get_broadcasts_collection, get_voices_collection
from app.generator import (
    generate_script,
    get_audio_duration,
    get_voice_audio_path,
    new_broadcast_audio_path,
)
from app.llm import resolve_provider
from app.personas import pacing_for_voice
from app.tts import (
    VoiceRender,
    clone_minimax_voice,
    render_episode,
    resolve_engine,
    speakable,
)

logger = logging.getLogger("radionow.broadcasts")

# Serialize renders so the daily job and admin requests don't compete.
_render_lock = asyncio.Lock()


def engine_for_voice(voice: dict, override: str | None = None) -> str:
    return resolve_engine(override or voice.get("tts_engine"))


async def ensure_minimax_voice_id(
    voice: dict, tracker: CostTracker, force_new: bool = False
) -> str:
    """Return the voice's MiniMax voice id, cloning (and storing) it if needed."""
    if voice.get("minimax_voice_id") and not force_new:
        return voice["minimax_voice_id"]
    audio_path = get_voice_audio_path(voice["audio_filename"])
    voice_id = await asyncio.to_thread(clone_minimax_voice, audio_path, tracker)
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
    tracker: CostTracker,
    jingles: bool,
):
    """Render the script to an MP3 and return (path, duration_seconds)."""
    output_path = new_broadcast_audio_path()
    pause_scale, speed = pacing_for_voice(voice)
    cfg = VoiceRender(
        engine=engine,
        voice_audio_path=get_voice_audio_path(voice["audio_filename"]),
        reference_text=voice["reference_text"],
        style_instruction=style_instruction,
        pause_scale=pause_scale,
        speed=speed,
    )

    async def render():
        await asyncio.to_thread(render_episode, script, output_path, cfg, tracker, jingles)

    try:
        async with _render_lock:
            if engine == "minimax":
                cfg.minimax_voice_id = await ensure_minimax_voice_id(voice, tracker)
                try:
                    await render()
                except Exception as e:
                    # Cloned voices can expire upstream; re-clone once and retry.
                    logger.warning("MiniMax render failed (%s); re-cloning voice", e)
                    cfg.minimax_voice_id = await ensure_minimax_voice_id(
                        voice, tracker, force_new=True
                    )
                    await render()
            else:
                await render()
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
    jingles: bool = True,
    extra_fields: dict | None = None,
) -> dict:
    """Generate script and audio, store the broadcast, return the document."""
    provider = resolve_provider(llm_provider)
    engine = engine_for_voice(voice, tts_engine)
    style_instruction = style_override or voice.get("style_instruction")
    tracker = CostTracker()

    title, script, summary, usage = await generate_script(prompt, provider)
    tracker.add_llm(usage["model"], usage["input_tokens"], usage["output_tokens"])
    audio_path, duration_seconds = await render_for_voice(
        voice, script, engine, style_instruction, tracker, jingles
    )

    topic = input_text[:50].strip() + ("..." if len(input_text) > 50 else "")
    doc = {
        "title": title,
        "voice_id": str(voice["_id"]),
        "topic": topic,
        "input_text": input_text,
        "generated_script": speakable(script),
        "script_markup": script,
        "summary": summary,
        "audio_filename": audio_path.name,
        "duration_seconds": duration_seconds,
        "style_override": style_override,
        "llm_provider": provider,
        "llm_model": usage["model"],
        "tts_engine": engine,
        "jingles": jingles,
        "cost": tracker.summary(),
        "kind": "manual",
        "created_at": datetime.utcnow(),
        **(extra_fields or {}),
    }
    result = await get_broadcasts_collection().insert_one(doc)
    doc["_id"] = result.inserted_id
    logger.info(
        "Created broadcast: %s (%s, %s/%s, $%.3f)",
        title, doc["_id"], provider, engine, doc["cost"]["total_usd"],
    )
    return doc
