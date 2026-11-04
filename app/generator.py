"""Script prompts, LLM response parsing and audio/voice file helpers."""

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from wave import Error as WaveError, open as wave_open

from mutagen import File as MutagenFile

from app.llm import generate_script_json
from app.tts import estimate_words_for_minutes

logger = logging.getLogger("radionow.generator")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
if REPLICATE_API_TOKEN:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN

VOICES_DIR = Path("/app/voices")
AUDIO_DIR = Path("/app/audio")
VOICES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def length_requirements(target_minutes: int) -> str:
    minutes = max(1, min(int(target_minutes), 20))
    words = estimate_words_for_minutes(minutes)
    low, high = int(words * 0.85), int(words * 1.15)
    return (
        f"- Spoken length: about {minutes} minutes, i.e. {low}-{high} words. "
        "Stay inside this range."
    )


def speech_rules(cues_supported: bool) -> str:
    cue_rule = (
        "- You may use the cues (laughs) or (sighs) inline, sparingly (at most "
        "one every couple of paragraphs), where the speaker would naturally "
        "do so. No other stage directions."
        if cues_supported
        else "- No stage directions or sound cues of any kind."
    )
    return f"""\
- This will be read aloud by a voice clone, so write for the ear: spoken
  English, contractions, varied sentence length, natural asides and the small
  verbal habits of the speaker. No lists, headings, markdown or emojis.
- Use commas, ellipses and dashes for pauses and rhythm. Separate thoughts into
  paragraphs with a blank line; each paragraph is one breath of thought.
- Write numbers, dates and abbreviations the way they would be said aloud.
- No speaker labels, no meta-commentary, nothing that is not spoken.
{cue_rule}"""


OUTPUT_FORMAT = """\
OUTPUT FORMAT:
Return ONLY a JSON object with exactly these keys:
{
  "title": "short evocative episode title, max 8 words",
  "summary": "2-3 sentence teaser for the episode listing",
  "script": "the complete monologue, paragraphs separated by blank lines"
}"""


def build_script_prompt(
    input_text: str,
    twist: str | None,
    target_minutes: int,
    persona: str | None,
    cues_supported: bool,
) -> str:
    """Prompt for a free-form broadcast from the admin panel."""
    persona_section = ""
    if persona:
        persona_section = f"""
SPEAKER (write entirely in this person's voice and manner of speaking; capture
the style, never quote or recycle their famous lines):
{persona}
"""
    twist_section = f"\nSTYLE/TRANSFORMATION:\n{twist}\n" if twist else ""

    return f"""You are a master scriptwriter creating a spoken monologue for an \
audio broadcast on "Radio Now".
{persona_section}
INPUT/TOPIC:
{input_text}
{twist_section}
REQUIREMENTS:
{length_requirements(target_minutes)}
- Engaging, insightful and memorable; a clear arc with an opening hook and a
  satisfying close.
{speech_rules(cues_supported)}

{OUTPUT_FORMAT}"""


async def generate_script(prompt: str, provider: str | None) -> tuple[str, str, str]:
    """Run a script prompt through the chosen LLM; return (title, script, summary)."""
    raw = await generate_script_json(prompt, provider)
    title, script, summary = parse_script_response(raw)
    logger.info("Generated script: %s (%d chars)", title, len(script))
    return title, script, summary


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_script_response(response_text: str) -> tuple[str, str, str]:
    """Parse LLM response into title, script, and summary."""
    json_payload = _extract_json_payload(response_text)
    if json_payload:
        try:
            data = json.loads(json_payload)
            title = str(data.get("title", "")).strip().strip('"').strip("*")
            script = str(data.get("script", "")).strip()
            summary = str(data.get("summary", "")).strip()
            if not script:
                raise ValueError("Missing script in JSON response")
            return title or "Untitled Broadcast", script, normalize_summary(
                summary, script
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse JSON script response: %s", e)

    lines = response_text.strip().split("\n", 2)
    if len(lines) >= 3:
        title = lines[0].strip().strip('"').strip("*") or "Untitled Broadcast"
        script = lines[2].strip()
    else:
        title, script = "Untitled Broadcast", response_text.strip()
    return title, script, normalize_summary("", script)


def _extract_json_payload(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start_idx, end_idx = stripped.find("{"), stripped.rfind("}")
    if start_idx == -1 or end_idx <= start_idx:
        return None
    return stripped[start_idx : end_idx + 1]


def normalize_summary(summary: str, script: str) -> str:
    clean = " ".join(summary.split()) or " ".join(script.split())
    return truncate_to_sentences(clean, max_sentences=3)


def truncate_to_sentences(text: str, max_sentences: int = 3) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return " ".join(parts[:max_sentences])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def new_broadcast_audio_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return AUDIO_DIR / f"{timestamp}_{uuid.uuid4().hex[:8]}.mp3"


def get_audio_duration(audio_path: Path) -> int:
    """Get duration of audio file in seconds."""
    try:
        audio = MutagenFile(audio_path)
        if audio and audio.info:
            return max(0, int(round(float(audio.info.length))))
    except Exception as e:
        logger.warning("Could not determine audio duration: %s", e)
    try:
        with wave_open(str(audio_path), "rb") as wav_file:
            if wav_file.getframerate() > 0:
                return int(round(wav_file.getnframes() / wav_file.getframerate()))
    except (WaveError, OSError) as e:
        logger.warning("WAV duration fallback failed: %s", e)
    return 0


def save_voice_audio(audio_data: bytes, voice_slug: str) -> str:
    """Save uploaded voice reference audio; return its relative filename."""
    voice_dir = VOICES_DIR / voice_slug
    voice_dir.mkdir(parents=True, exist_ok=True)
    file_path = voice_dir / f"{voice_slug}_{uuid.uuid4().hex[:8]}.wav"
    file_path.write_bytes(audio_data)
    logger.info("Saved voice audio: %s", file_path)
    return f"{voice_slug}/{file_path.name}"


def get_voice_audio_path(audio_filename: str) -> Path:
    return VOICES_DIR / audio_filename


def delete_voice_audio(audio_filename: str) -> bool:
    try:
        file_path = VOICES_DIR / audio_filename
        if file_path.exists():
            file_path.unlink()
            parent = file_path.parent
            if parent != VOICES_DIR and not any(parent.iterdir()):
                parent.rmdir()
            logger.info("Deleted voice audio: %s", audio_filename)
            return True
    except Exception as e:
        logger.error("Failed to delete voice audio %s: %s", audio_filename, e)
    return False


def delete_broadcast_audio(audio_filename: str) -> bool:
    try:
        file_path = AUDIO_DIR / audio_filename
        if file_path.exists():
            file_path.unlink()
            logger.info("Deleted broadcast audio: %s", audio_filename)
            return True
    except Exception as e:
        logger.error("Failed to delete broadcast audio %s: %s", audio_filename, e)
    return False
