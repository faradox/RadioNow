"""Script prompts, LLM response parsing and audio/voice file helpers."""

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from wave import Error as WaveError, open as wave_open

from mutagen import File as MutagenFile

from app.llm import generate_script_json

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


# Words per spoken minute, counting the pauses the hosts take.
PROMPT_WPM = 130


def length_requirements(target_minutes: int) -> str:
    minutes = max(1, min(int(target_minutes), 20))
    words = minutes * PROMPT_WPM
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
- Separate thoughts into paragraphs with a blank line; each paragraph is one
  movement of thought.
- TIMING IS HALF THE ART. Mark silences explicitly: write [pause] (a beat)
  or [long pause] (letting something sink in) in the text. Put a [pause]
  after a punchline, before a reveal, after a rhetorical question, and
  wherever the speaker would stop to let the listener catch up; one to three
  per paragraph. Use [long pause] a few times per episode, after the big
  ideas. Build-ups can run long; the line that lands should be short and be
  followed by a pause. Also use commas, ellipses and dashes for smaller
  hesitations.
- Write numbers, dates and abbreviations the way they would be said aloud.
- No speaker labels, no meta-commentary, nothing that is not spoken.
{cue_rule}"""


OUTPUT_FORMAT = """\
OUTPUT FORMAT:
Return ONLY a JSON object with exactly these keys:
{
  "title": "short evocative episode title, max 8 words",
  "summary": "2-3 sentence teaser for the episode listing",
  "motifs": ["the 3-6 central images, metaphors and running jokes of the
             script, a few words each"],
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


@dataclass
class Script:
    title: str
    script: str  # keeps its [pause] markers; strip them for display
    summary: str
    motifs: list[str] = field(default_factory=list)
    model: str = ""


LLM_ATTEMPTS = 2


async def generate_script(prompt: str, provider: str | None, tracker) -> Script:
    """Run a script prompt and parse the result; one retry on malformed output.

    Every attempt's token usage is added to `tracker` (a CostTracker).
    """
    for attempt in range(1, LLM_ATTEMPTS + 1):
        raw, usage = await generate_script_json(prompt, provider)
        tracker.add_llm(usage["model"], usage["input_tokens"], usage["output_tokens"])
        try:
            result = parse_script_response(raw)
        except ValueError as e:
            logger.warning("Malformed script response (attempt %d): %s", attempt, e)
            if attempt == LLM_ATTEMPTS:
                raise ValueError(f"LLM returned a malformed script twice: {e}") from e
            continue
        logger.info("Generated script: %s (%d chars)", result.title, len(result.script))
        result.model = usage["model"]
        return result
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

SCRIPT_KEYS = ("title", "summary", "motifs", "script")


def parse_script_response(response_text: str) -> Script:
    """Parse the LLM's JSON answer; raise ValueError if it isn't usable.

    Never falls back to treating raw JSON as the script: that would have the
    host read the braces and keys out loud.
    """
    payload = _extract_json_payload(response_text)
    if payload is None:
        raise ValueError("no JSON object in the response")
    data = _loads_lenient(payload)
    script = str(data.get("script") or "").strip()
    if len(script.split()) < 30:
        raise ValueError("script missing or too short")
    if re.search(r'"(title|summary|script|motifs)"\s*:', script):
        raise ValueError("script contains JSON keys")
    title = str(data.get("title") or "").strip().strip('"').strip("*")
    if not re.search(r"\w", title):
        title = "Untitled Broadcast"
    motifs = data.get("motifs") or []
    if isinstance(motifs, str):
        motifs = [m for m in re.split(r"[;\n]", motifs)]
    motifs = [str(m).strip() for m in motifs if str(m).strip()][:8]
    summary = normalize_summary(str(data.get("summary") or ""), speakable_text(script))
    return Script(title=title[:120], script=script, summary=summary, motifs=motifs)


def _loads_lenient(payload: str) -> dict:
    """json.loads, tolerating raw newlines in strings, then a key-split repair."""
    try:
        data = json.loads(payload, strict=False)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as e:
        logger.warning("JSON decode failed (%s); trying key-split repair", e)
    return _repair_by_keys(payload)


def _repair_by_keys(payload: str) -> dict:
    """Recover string fields from almost-JSON (e.g. unescaped quotes)."""
    key_re = re.compile(r'"(%s)"\s*:\s*' % "|".join(SCRIPT_KEYS))
    matches = list(key_re.finditer(payload))
    if not matches:
        raise ValueError("unparseable JSON")
    data: dict = {}
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(payload)
        value = payload[m.end() : end].strip().rstrip("}").strip().rstrip(",").strip()
        if m.group(1) == "motifs":
            data["motifs"] = re.findall(r'"((?:[^"\\]|\\.)*)"', value)
            continue
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        try:
            data[m.group(1)] = json.loads(f'"{value}"', strict=False)
        except json.JSONDecodeError:
            data[m.group(1)] = value.replace("\\n", "\n").replace('\\"', '"')
    return data


def _extract_json_payload(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start_idx, end_idx = stripped.find("{"), stripped.rfind("}")
    if start_idx == -1 or end_idx <= start_idx:
        return None
    return stripped[start_idx : end_idx + 1]


def speakable_text(script: str) -> str:
    return re.sub(r"\s*\[(?:long )?pause\]\s*", " ", script)


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
