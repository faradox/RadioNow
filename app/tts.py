"""Text-to-speech engines (Replicate) and audio assembly.

Every engine returns arbitrary encoded audio. It is decoded with ffmpeg to raw
PCM in one common format, stitched with short gaps, then encoded once to a
loudness-normalized MP3. All functions here are blocking; call them through
`asyncio.to_thread` from async code.
"""

import logging
import os
import re
import subprocess
from pathlib import Path

import replicate

logger = logging.getLogger("radionow.tts")

QWEN_MODEL = os.getenv("QWEN_TTS_MODEL", "qwen/qwen3-tts")
MINIMAX_TTS_MODEL = os.getenv("MINIMAX_TTS_MODEL", "minimax/speech-2.8-hd")
MINIMAX_CLONE_MODEL = os.getenv("MINIMAX_CLONE_MODEL", "minimax/voice-cloning")
# Target model family passed to the cloning job.
MINIMAX_CLONE_TARGET = os.getenv("MINIMAX_CLONE_TARGET", "speech-02-hd")

ENGINES = {
    "qwen3": "Qwen3-TTS (zero-shot clone per request)",
    "minimax": "MiniMax Speech HD (cloned voice id)",
}
DEFAULT_ENGINE = "qwen3"

# Max characters per request, kept under each engine's limit.
ENGINE_MAX_CHARS = {"qwen3": 4000, "minimax": 9000}

SAMPLE_RATE = 44100
SAMPLE_WIDTH = 2  # 16-bit mono PCM
CHUNK_GAP_SECONDS = 0.6
PARAGRAPH_PAUSE_SECONDS = 0.8

# Delivery cues the script LLM may emit; only MiniMax renders them as sounds.
CUE_PATTERN = re.compile(r"\((laughs|chuckles|sighs)\)", re.IGNORECASE)


def resolve_engine(engine: str | None) -> str:
    engine = (engine or "").strip().lower()
    return engine if engine in ENGINES else DEFAULT_ENGINE


def engine_supports_cues(engine: str) -> bool:
    return resolve_engine(engine) == "minimax"


def prepare_text(text: str, engine: str) -> str:
    """Adapt script text to what the engine understands."""
    if engine == "minimax":
        text = CUE_PATTERN.sub(
            lambda m: "(laughs)" if m.group(1).lower() != "sighs" else "(sighs)",
            text,
        )
        # MiniMax pause marker between paragraphs.
        return re.sub(r"\n\s*\n+", f" <#{PARAGRAPH_PAUSE_SECONDS}#> ", text.strip())
    text = re.sub(r"[ \t]*" + CUE_PATTERN.pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def _read_output(output) -> bytes:
    if hasattr(output, "read"):
        return output.read()
    if isinstance(output, (list, tuple)) and output and hasattr(output[0], "read"):
        return output[0].read()
    raise ValueError(f"Unexpected output format from Replicate: {type(output)}")


def synthesize_qwen(
    text: str,
    voice_audio_path: Path,
    reference_text: str,
    style_instruction: str | None,
) -> bytes:
    tts_input = {"mode": "voice_clone", "text": text, "reference_text": reference_text}
    if style_instruction:
        tts_input["style_instruction"] = style_instruction
    with open(voice_audio_path, "rb") as audio_file:
        tts_input["reference_audio"] = audio_file
        return _read_output(replicate.run(QWEN_MODEL, input=tts_input))


def clone_minimax_voice(voice_audio_path: Path) -> str:
    """Create a MiniMax cloned voice from the reference audio; return voice_id."""
    with open(voice_audio_path, "rb") as audio_file:
        output = replicate.run(
            MINIMAX_CLONE_MODEL,
            input={
                "voice_file": audio_file,
                "model": MINIMAX_CLONE_TARGET,
                "need_noise_reduction": True,
                "need_volume_normalization": True,
            },
        )
    voice_id = output.get("voice_id") if isinstance(output, dict) else None
    if not voice_id:
        raise ValueError(f"MiniMax voice cloning returned no voice_id: {output!r}")
    logger.info("Created MiniMax voice %s", voice_id)
    return voice_id


def synthesize_minimax(text: str, voice_id: str) -> bytes:
    output = replicate.run(
        MINIMAX_TTS_MODEL,
        input={
            "text": text,
            "voice_id": voice_id,
            "emotion": "auto",
            "sample_rate": 44100,
            "bitrate": 256000,
            "channel": "mono",
            "language_boost": "English",
            "english_normalization": True,
        },
    )
    return _read_output(output)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def estimate_words_for_minutes(minutes: float) -> int:
    """Estimate words for spoken audio using a moderate speaking pace."""
    return max(80, int(round(float(minutes) * 150)))


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def split_paragraphs(script: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n+", script) if p.strip()]
    return parts or ([script.strip()] if script.strip() else [])


def split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Split an oversized paragraph on sentence boundaries."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if current and len(current) + len(sentence) + 1 > max_chars:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current)
    return out


def chunk_script(script: str, target_words: int | None, max_chars: int) -> list[str]:
    """Paragraph-aligned chunks near target_words (None = as few as possible)."""
    paragraphs = []
    for paragraph in split_paragraphs(script):
        paragraphs.extend(split_long_paragraph(paragraph, max_chars))

    chunks: list[str] = []
    current: list[str] = []
    words = chars = 0
    for paragraph in paragraphs:
        p_words, p_chars = count_words(paragraph), len(paragraph) + 2
        over_words = target_words is not None and words + p_words > target_words
        if current and (over_words or chars + p_chars > max_chars):
            chunks.append("\n\n".join(current))
            current, words, chars = [], 0, 0
        current.append(paragraph)
        words += p_words
        chars += p_chars
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ---------------------------------------------------------------------------
# Audio assembly (ffmpeg)
# ---------------------------------------------------------------------------


def decode_to_pcm(audio_data: bytes) -> bytes:
    """Decode any audio container/codec to raw 16-bit mono PCM."""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
        ],
        input=audio_data,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise ValueError(f"ffmpeg decode failed: {result.stderr.decode()[:300]}")
    return result.stdout


def encode_mp3(pcm_chunks: list[bytes], output_path: Path) -> None:
    """Join PCM chunks with short gaps and write a loudness-normalized MP3."""
    gap = b"\x00" * (int(CHUNK_GAP_SECONDS * SAMPLE_RATE) * SAMPLE_WIDTH)
    pcm = gap.join(pcm_chunks)
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", str(SAMPLE_RATE),
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(output_path),
        ],
        input=pcm,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ffmpeg encode failed: {result.stderr.decode()[:300]}")


def render_speech(
    script: str,
    output_path: Path,
    engine: str,
    *,
    voice_audio_path: Path,
    reference_text: str,
    style_instruction: str | None = None,
    minimax_voice_id: str | None = None,
    chunk_minutes: float | None = 1.0,
) -> None:
    """Synthesize a full script into output_path (MP3). Blocking."""
    engine = resolve_engine(engine)
    target_words = (
        estimate_words_for_minutes(min(max(chunk_minutes, 0.25), 10.0))
        if chunk_minutes
        else None
    )
    chunks = chunk_script(script, target_words, ENGINE_MAX_CHARS[engine])
    if not chunks:
        raise ValueError("No text to synthesize")

    logger.info("Rendering %d chunk(s) with %s", len(chunks), engine)
    pcm_chunks = []
    for idx, chunk in enumerate(chunks, start=1):
        logger.info("Synthesizing chunk %d/%d", idx, len(chunks))
        text = prepare_text(chunk, engine)
        if engine == "minimax":
            if not minimax_voice_id:
                raise ValueError("MiniMax engine needs a cloned voice id")
            audio = synthesize_minimax(text, minimax_voice_id)
        else:
            audio = synthesize_qwen(
                text, voice_audio_path, reference_text, style_instruction
            )
        pcm_chunks.append(decode_to_pcm(audio))

    encode_mp3(pcm_chunks, output_path)
