import logging
import json
import os
import re
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from wave import Error as WaveError, open as wave_open

import replicate
import google.generativeai as genai
from mutagen import File as MutagenFile

logger = logging.getLogger("radionow.generator")

# Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

VOICES_DIR = Path("/app/voices")
AUDIO_DIR = Path("/app/audio")

# Ensure directories exist
VOICES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def configure_apis():
    """Configure API clients."""
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    if REPLICATE_API_TOKEN:
        os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN


async def generate_script(
    input_text: str,
    twist: str | None = None,
    target_minutes: int = 4,
) -> tuple[str, str, str]:
    """
    Generate a speech script using Gemini.

    Args:
        input_text: The topic, mood, or content to generate a script about
        twist: Optional transformation/style to apply to the content
        target_minutes: Target length of spoken script in minutes

    Returns:
        tuple of (title, script_text, summary)
    """
    configure_apis()

    # Build the prompt
    twist_section = ""
    if twist:
        twist_section = f"""
STYLE/TRANSFORMATION:
Apply this creative style or transformation to the content: {twist}
"""

    bounded_minutes = max(1, min(int(target_minutes), 20))
    target_words = estimate_words_for_minutes(bounded_minutes)
    min_words = max(120, int(target_words * 0.8))
    max_words = max(min_words + 50, int(target_words * 1.2))
    min_duration = max(1, bounded_minutes - 1)
    max_duration = bounded_minutes + 1

    prompt = f"""You are a master scriptwriter creating a spoken monologue for an audio broadcast.

INPUT/TOPIC:
{input_text}
{twist_section}
REQUIREMENTS:
- Target spoken length: about {bounded_minutes} minutes
- Approximate word target: {target_words} words (acceptable range: {min_words}-{max_words})
- Keep duration roughly within {min_duration}-{max_duration} minutes when read naturally
- The text should flow naturally when read aloud
- Include natural pauses (use ellipses or dashes for dramatic effect)
- Make it engaging, insightful, and memorable
- Do NOT include stage directions, speaker labels, or meta-commentary
- Write only the words to be spoken
- Also write a short teaser summary of the monologue in 2-3 sentences maximum

OUTPUT FORMAT:
Return ONLY valid JSON with this exact shape:
{{
  "title": "short evocative title, max 8 words",
  "summary": "2-3 sentence teaser summary",
  "script": "complete monologue text"
}}

Begin now:"""

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = await model.generate_content_async(prompt)

        if not response.text:
            raise ValueError("Empty response from Gemini")

        title, script, summary = parse_script_response(response.text)
        logger.info(
            "Generated script: %s (%d chars, summary %d chars)",
            title,
            len(script),
            len(summary),
        )
        return title, script, summary

    except Exception as e:
        logger.error("Failed to generate script: %s", e)
        raise


async def generate_audio(
    script: str,
    voice_audio_path: Path,
    reference_text: str,
    style_instruction: str | None = None,
) -> tuple[Path, int]:
    """
    Generate audio using Replicate qwen3-tts with voice cloning.

    Args:
        script: The text to synthesize
        voice_audio_path: Path to reference audio for voice cloning
        reference_text: Transcript of the reference audio
        style_instruction: Optional style instruction for TTS (e.g., speaking style, emotion)

    Returns:
        tuple of (audio_file_path, duration_seconds)
    """
    configure_apis()

    # Generate unique filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_id = uuid.uuid4().hex[:8]
    output_filename = f"{timestamp}_{audio_id}.wav"
    output_path = AUDIO_DIR / output_filename

    try:
        logger.info("Starting TTS generation with voice cloning...")
        audio_data = synthesize_tts_audio_bytes(
            text=script,
            voice_audio_path=voice_audio_path,
            reference_text=reference_text,
            style_instruction=style_instruction,
        )

        # Write audio file
        with open(output_path, "wb") as f:
            f.write(audio_data)

        # Get duration using mutagen
        duration_seconds = get_audio_duration(output_path)

        logger.info(
            "Generated audio: %s (%d seconds)", output_filename, duration_seconds
        )
        return output_path, duration_seconds

    except Exception as e:
        logger.error("Failed to generate audio: %s", e)
        # Clean up partial file if exists
        if output_path.exists():
            output_path.unlink()
        raise


async def generate_audio_chunked(
    script: str,
    voice_audio_path: Path,
    reference_text: str,
    style_instruction: str | None = None,
    chunk_minutes: float = 1.0,
    silence_seconds: float = 1.5,
) -> tuple[Path, int]:
    """Generate chunked audio and stitch chunks with short silences."""
    configure_apis()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_id = uuid.uuid4().hex[:8]
    output_filename = f"{timestamp}_{audio_id}.wav"
    output_path = AUDIO_DIR / output_filename

    bounded_chunk_minutes = min(max(chunk_minutes, 0.25), 10.0)
    chunk_target_words = estimate_words_for_minutes(bounded_chunk_minutes)
    paragraphs = split_script_into_paragraphs(script)
    chunks = chunk_paragraphs(paragraphs, chunk_target_words)

    if not chunks:
        raise ValueError("No text chunks available for TTS")

    logger.info(
        "Starting chunked TTS generation: %d chunks (~%d words/chunk)",
        len(chunks),
        chunk_target_words,
    )

    try:
        chunk_frames: list[bytes] = []
        chunk_params = None

        for idx, chunk_text in enumerate(chunks, start=1):
            logger.info("Generating chunk %d/%d", idx, len(chunks))
            audio_data = synthesize_tts_audio_bytes(
                text=chunk_text,
                voice_audio_path=voice_audio_path,
                reference_text=reference_text,
                style_instruction=style_instruction,
            )
            params, frames = decode_wav_bytes(audio_data)
            if chunk_params is None:
                chunk_params = params
            elif not wav_params_compatible(chunk_params, params):
                raise ValueError(
                    "Audio chunk format mismatch; cannot concatenate WAV chunks"
                )
            chunk_frames.append(frames)

        if chunk_params is None:
            raise ValueError("No audio chunks generated")

        write_stitched_wav(
            output_path=output_path,
            wav_params=chunk_params,
            chunk_frames=chunk_frames,
            silence_seconds=silence_seconds,
        )
        duration_seconds = get_audio_duration(output_path)
        logger.info(
            "Generated chunked audio: %s (%d seconds)",
            output_filename,
            duration_seconds,
        )
        return output_path, duration_seconds

    except Exception as e:
        logger.error("Failed to generate chunked audio: %s", e)
        if output_path.exists():
            output_path.unlink()
        raise


def get_audio_duration(audio_path: Path) -> int:
    """Get duration of audio file in seconds."""
    try:
        audio = MutagenFile(audio_path)
        if audio and audio.info:
            return max(0, int(round(float(audio.info.length))))
    except Exception as e:
        logger.warning("Could not determine audio duration: %s", e)

    # Fallback for plain WAV files.
    try:
        with wave_open(str(audio_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            if frame_rate > 0:
                return max(0, int(round(frame_count / float(frame_rate))))
    except (WaveError, OSError) as e:
        logger.warning("WAV duration fallback failed: %s", e)

    return 0


def parse_script_response(response_text: str) -> tuple[str, str, str]:
    """Parse LLM response into title, script, and summary."""
    json_payload = _extract_json_payload(response_text)
    if json_payload:
        try:
            data = json.loads(json_payload)
            title = str(data.get("title", "")).strip().strip('"').strip("*")
            script = str(data.get("script", "")).strip()
            summary = str(data.get("summary", "")).strip()
            if not title:
                title = "Untitled Broadcast"
            if not script:
                raise ValueError("Missing script in JSON response")
            summary = normalize_summary(summary, script)
            return title, script, summary
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to parse Gemini JSON response: %s", e)

    # Backward-compatible fallback parser.
    lines = response_text.strip().split("\n", 2)
    if len(lines) >= 3:
        title = lines[0].strip().strip('"').strip("*") or "Untitled Broadcast"
        script = lines[2].strip()
    else:
        title = "Untitled Broadcast"
        script = response_text.strip()
    summary = normalize_summary("", script)
    return title, script, summary


def _extract_json_payload(text: str) -> str | None:
    """Extract a JSON object payload from plain text or fenced output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start_idx = stripped.find("{")
    end_idx = stripped.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    return stripped[start_idx : end_idx + 1]


def normalize_summary(summary: str, script: str) -> str:
    """Normalize summary to a short 2-3 sentence teaser."""
    clean = " ".join(summary.split())
    if not clean:
        clean = build_summary_from_script(script)
    return truncate_to_sentences(clean, max_sentences=3)


def build_summary_from_script(script: str) -> str:
    """Generate a fallback teaser from the script when summary is missing."""
    return truncate_to_sentences(" ".join(script.split()), max_sentences=3)


def truncate_to_sentences(text: str, max_sentences: int = 3) -> str:
    """Keep only the first few sentence-like segments."""
    sentence_parts = re.split(r"(?<=[.!?])\s+", text.strip())
    sentence_parts = [part.strip() for part in sentence_parts if part.strip()]
    if not sentence_parts:
        return ""
    return " ".join(sentence_parts[:max_sentences])


def estimate_words_for_minutes(minutes: float) -> int:
    """Estimate words for spoken audio using a moderate speaking pace."""
    return max(80, int(round(float(minutes) * 170)))


def split_script_into_paragraphs(script: str) -> list[str]:
    """Split script into cleaned paragraphs."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", script) if part.strip()]
    if paragraphs:
        return paragraphs
    single_block = script.strip()
    return [single_block] if single_block else []


def chunk_paragraphs(paragraphs: list[str], target_words: int) -> list[str]:
    """Build paragraph-aligned chunks close to target word count."""
    chunks: list[str] = []
    current_parts: list[str] = []
    current_word_count = 0

    for paragraph in paragraphs:
        paragraph_words = count_words(paragraph)
        if current_parts and current_word_count + paragraph_words > target_words:
            chunks.append("\n\n".join(current_parts))
            current_parts = [paragraph]
            current_word_count = paragraph_words
            continue
        current_parts.append(paragraph)
        current_word_count += paragraph_words

    if current_parts:
        chunks.append("\n\n".join(current_parts))
    return chunks


def count_words(text: str) -> int:
    """Count words in text for chunk size estimation."""
    return len(re.findall(r"\b[\w'-]+\b", text))


def synthesize_tts_audio_bytes(
    text: str,
    voice_audio_path: Path,
    reference_text: str,
    style_instruction: str | None = None,
) -> bytes:
    """Generate TTS audio bytes for a script chunk."""
    tts_input = {
        "mode": "voice_clone",
        "text": text,
        "reference_text": reference_text,
    }
    if style_instruction:
        tts_input["style_instruction"] = style_instruction

    with open(voice_audio_path, "rb") as audio_file:
        tts_input["reference_audio"] = audio_file
        output = replicate.run("qwen/qwen3-tts", input=tts_input)

    if hasattr(output, "read"):
        return output.read()
    if isinstance(output, (list, tuple)) and len(output) > 0 and hasattr(output[0], "read"):
        return output[0].read()
    raise ValueError(f"Unexpected output format from Replicate: {type(output)}")


def decode_wav_bytes(audio_data: bytes):
    """Decode WAV bytes into params + raw frames."""
    with wave_open(BytesIO(audio_data), "rb") as wav_file:
        params = wav_file.getparams()
        frames = wav_file.readframes(wav_file.getnframes())
    return params, frames


def wav_params_compatible(first_params, next_params) -> bool:
    """Check whether two WAV streams can be concatenated directly."""
    return (
        first_params.nchannels == next_params.nchannels
        and first_params.sampwidth == next_params.sampwidth
        and first_params.framerate == next_params.framerate
        and first_params.comptype == next_params.comptype
    )


def write_stitched_wav(
    output_path: Path,
    wav_params,
    chunk_frames: list[bytes],
    silence_seconds: float,
) -> None:
    """Write chunk frames to a final WAV file with silence in between."""
    frame_bytes = wav_params.nchannels * wav_params.sampwidth
    silence_frame_count = int(round(max(0.0, silence_seconds) * wav_params.framerate))
    silence_bytes = b"\x00" * (silence_frame_count * frame_bytes)

    with wave_open(str(output_path), "wb") as wav_out:
        wav_out.setnchannels(wav_params.nchannels)
        wav_out.setsampwidth(wav_params.sampwidth)
        wav_out.setframerate(wav_params.framerate)
        wav_out.setcomptype(wav_params.comptype, wav_params.compname)

        for idx, frames in enumerate(chunk_frames):
            wav_out.writeframes(frames)
            if idx < len(chunk_frames) - 1 and silence_bytes:
                wav_out.writeframes(silence_bytes)


def save_voice_audio(audio_data: bytes, voice_slug: str) -> str:
    """
    Save uploaded voice reference audio.

    Returns:
        The filename of the saved audio.
    """
    audio_id = uuid.uuid4().hex[:8]
    filename = f"{voice_slug}_{audio_id}.wav"
    voice_dir = VOICES_DIR / voice_slug
    voice_dir.mkdir(parents=True, exist_ok=True)

    file_path = voice_dir / filename
    with open(file_path, "wb") as f:
        f.write(audio_data)

    logger.info("Saved voice audio: %s", file_path)
    return f"{voice_slug}/{filename}"


def get_voice_audio_path(audio_filename: str) -> Path:
    """Get the full path to a voice's reference audio."""
    return VOICES_DIR / audio_filename


def delete_voice_audio(audio_filename: str) -> bool:
    """Delete a voice's reference audio file."""
    try:
        file_path = VOICES_DIR / audio_filename
        if file_path.exists():
            file_path.unlink()
            # Try to remove the directory if empty
            parent = file_path.parent
            if parent != VOICES_DIR and not any(parent.iterdir()):
                parent.rmdir()
            logger.info("Deleted voice audio: %s", audio_filename)
            return True
    except Exception as e:
        logger.error("Failed to delete voice audio %s: %s", audio_filename, e)
    return False


def delete_broadcast_audio(audio_filename: str) -> bool:
    """Delete a broadcast's generated audio file."""
    try:
        file_path = AUDIO_DIR / audio_filename
        if file_path.exists():
            file_path.unlink()
            logger.info("Deleted broadcast audio: %s", audio_filename)
            return True
    except Exception as e:
        logger.error("Failed to delete broadcast audio %s: %s", audio_filename, e)
    return False
