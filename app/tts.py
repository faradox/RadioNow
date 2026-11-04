"""Text-to-speech engines (Replicate) and episode rendering.

The script is split into segments at the LLM's [pause] / [long pause] markers,
paragraph breaks and (for long passages) sentence boundaries. Each segment is
synthesized separately, trimmed, and its natural pauses stretched; segments
are then joined with exact silences. This gives the host real timing instead
of one breathless read. All functions here are blocking.
"""

import logging
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import replicate

from app import audio
from app.bowls import pick_jingles
from app.costs import CostTracker

logger = logging.getLogger("radionow.tts")

QWEN_MODEL = os.getenv("QWEN_TTS_MODEL", "qwen/qwen3-tts")
MINIMAX_TTS_MODEL = os.getenv("MINIMAX_TTS_MODEL", "minimax/speech-2.8-hd")
MINIMAX_CLONE_MODEL = os.getenv("MINIMAX_CLONE_MODEL", "minimax/voice-cloning")
# Target model family passed to the cloning job. minimax/voice-cloning accepts
# speech-2.6-hd/turbo and speech-02-hd/turbo (no 2.8 option as of 2026-11).
MINIMAX_CLONE_TARGET = os.getenv("MINIMAX_CLONE_TARGET", "speech-2.6-hd")

ENGINES = {
    "qwen3": "Qwen3-TTS (zero-shot clone per request)",
    "minimax": "MiniMax Speech HD (cloned voice id)",
}
DEFAULT_ENGINE = "qwen3"

# Max characters per segment; long paragraphs split on sentences.
ENGINE_MAX_CHARS = {"qwen3": 900, "minimax": 2500}
MIN_SEGMENT_WORDS = 2  # short punchlines keep their own beat
PARALLEL_REQUESTS = 4

# Base pause lengths in seconds, multiplied by the voice's pause scale.
PAUSES = {"sentence": 0.45, "pause": 1.1, "paragraph": 1.5, "long pause": 2.4}
MAX_PAUSE_SECONDS = 4.0

PAUSE_MARKER = re.compile(r"\[(long pause|pause)\]", re.IGNORECASE)
# Delivery cues the script LLM may emit; only MiniMax renders them as sounds.
CUE_PATTERN = re.compile(r"\((laughs|chuckles|sighs)\)", re.IGNORECASE)


def resolve_engine(engine: str | None) -> str:
    engine = (engine or "").strip().lower()
    return engine if engine in ENGINES else DEFAULT_ENGINE


def engine_supports_cues(engine: str) -> bool:
    return resolve_engine(engine) == "minimax"


def prepare_text(text: str, engine: str) -> str:
    """Adapt segment text to what the engine understands."""
    text = PAUSE_MARKER.sub(" ", text)
    if engine == "minimax":
        text = CUE_PATTERN.sub(
            lambda m: "(sighs)" if m.group(1).lower() == "sighs" else "(laughs)", text
        )
    else:
        text = re.sub(r"[ \t]*" + CUE_PATTERN.pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()


def speakable(text: str) -> str:
    """Script text as it is spoken (no pause markers), for display/storage."""
    return re.sub(r"[ \t]{2,}", " ", PAUSE_MARKER.sub("", text)).replace(" \n", "\n")


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    text: str
    pause_after: float  # base seconds, before pause scaling


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def split_paragraphs(script: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n+", script) if p.strip()]
    return parts or ([script.strip()] if script.strip() else [])


def split_sentences(text: str, max_chars: int) -> list[str]:
    """Group sentences into pieces no longer than max_chars."""
    if len(text) <= max_chars:
        return [text]
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + len(sentence) + 1 > max_chars:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current)
    return out


def parse_segments(script: str, max_chars: int) -> list[Segment]:
    segments: list[Segment] = []
    for paragraph in split_paragraphs(script):
        parts = PAUSE_MARKER.split(paragraph)  # text, marker, text, marker, ...
        for i in range(0, len(parts), 2):
            text = parts[i].strip()
            marker = parts[i + 1].lower() if i + 1 < len(parts) else None
            if text:
                for piece in split_sentences(text, max_chars):
                    segments.append(Segment(piece, PAUSES["sentence"]))
                if marker:
                    segments[-1].pause_after = PAUSES[marker]
            elif marker and segments:
                segments[-1].pause_after = max(segments[-1].pause_after, PAUSES[marker])
        if segments:
            segments[-1].pause_after = max(segments[-1].pause_after, PAUSES["paragraph"])

    # Very short segments sound odd on their own: attach them to a neighbour.
    merged: list[Segment] = []
    for seg in segments:
        if (
            merged
            and count_words(seg.text) < MIN_SEGMENT_WORDS
            and len(merged[-1].text) + len(seg.text) < max_chars
        ):
            merged[-1].text = f"{merged[-1].text} {seg.text}"
            merged[-1].pause_after = seg.pause_after
        else:
            merged.append(seg)
    if len(merged) > 1 and count_words(merged[0].text) < MIN_SEGMENT_WORDS:
        merged[1].text = f"{merged[0].text} {merged[1].text}"
        merged.pop(0)
    return merged


# ---------------------------------------------------------------------------
# Engines (Replicate predictions, with usage metrics for cost tracking)
# ---------------------------------------------------------------------------


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def run_prediction(model: str, inputs: dict):
    """Run a Replicate prediction; return (output, predict_time seconds)."""
    prediction = replicate.models.predictions.create(model=model, input=inputs)
    prediction.wait()
    if prediction.status != "succeeded":
        raise RuntimeError(f"{model} {prediction.status}: {prediction.error}")
    metrics = prediction.metrics or {}
    return prediction.output, metrics.get("predict_time")


def _output_audio(output) -> bytes:
    if isinstance(output, (list, tuple)) and output:
        output = output[0]
    if hasattr(output, "read"):
        return output.read()
    if isinstance(output, str) and output.startswith("http"):
        return _download(output)
    raise ValueError(f"Unexpected TTS output from Replicate: {type(output)}")


def synthesize_qwen(
    text: str, voice_audio_path: Path, reference_text: str, style: str | None,
    tracker: CostTracker,
) -> bytes:
    inputs = {"mode": "voice_clone", "text": text, "reference_text": reference_text}
    if style:
        inputs["style_instruction"] = style
    with open(voice_audio_path, "rb") as audio_file:
        inputs["reference_audio"] = audio_file
        output, predict_time = run_prediction(QWEN_MODEL, inputs)
    tracker.add_replicate(QWEN_MODEL, "tts", chars=len(text), predict_time=predict_time)
    return _output_audio(output)


def synthesize_minimax(
    text: str, voice_id: str, speed: float, tracker: CostTracker
) -> bytes:
    output, predict_time = run_prediction(
        MINIMAX_TTS_MODEL,
        {
            "text": text,
            "voice_id": voice_id,
            "emotion": "auto",
            "speed": round(min(max(speed, 0.5), 2.0), 2),
            "sample_rate": 44100,
            "bitrate": 256000,
            "channel": "mono",
            "audio_format": "mp3",
            "language_boost": "English",
            "english_normalization": True,
        },
    )
    tracker.add_replicate(
        MINIMAX_TTS_MODEL, "tts", chars=len(text), predict_time=predict_time
    )
    return _output_audio(output)


def clone_minimax_voice(voice_audio_path: Path, tracker: CostTracker) -> str:
    """Create a MiniMax cloned voice from the reference audio; return voice_id."""
    with open(voice_audio_path, "rb") as audio_file:
        output, predict_time = run_prediction(
            MINIMAX_CLONE_MODEL,
            {
                "voice_file": audio_file,
                "model": MINIMAX_CLONE_TARGET,
                "need_noise_reduction": True,
                "need_volume_normalization": True,
            },
        )
    tracker.add_replicate(MINIMAX_CLONE_MODEL, "clone", predict_time=predict_time)
    voice_id = output.get("voice_id") if isinstance(output, dict) else None
    if not voice_id:
        raise ValueError(f"MiniMax voice cloning returned no voice_id: {output!r}")
    logger.info("Created MiniMax voice %s", voice_id)
    return voice_id


# ---------------------------------------------------------------------------
# Episode rendering
# ---------------------------------------------------------------------------


@dataclass
class VoiceRender:
    engine: str
    voice_audio_path: Path
    reference_text: str
    style_instruction: str | None = None
    minimax_voice_id: str | None = None
    pause_scale: float = 1.0
    speed: float = 1.0


def render_segment(seg: Segment, cfg: VoiceRender, tracker: CostTracker) -> np.ndarray:
    text = prepare_text(seg.text, cfg.engine)
    if cfg.engine == "minimax":
        if not cfg.minimax_voice_id:
            raise ValueError("MiniMax engine needs a cloned voice id")
        raw = synthesize_minimax(text, cfg.minimax_voice_id, cfg.speed, tracker)
        samples = audio.decode(raw)
    else:
        raw = synthesize_qwen(
            text, cfg.voice_audio_path, cfg.reference_text, cfg.style_instruction,
            tracker,
        )
        samples = audio.change_tempo(audio.decode(raw), cfg.speed)
    return audio.stretch_gaps(audio.trim(samples), cfg.pause_scale)


def render_episode(
    script: str,
    output_path: Path,
    cfg: VoiceRender,
    tracker: CostTracker,
    jingles: bool = True,
) -> None:
    """Synthesize the script, add jingles, write an MP3 to output_path."""
    segments = parse_segments(script, ENGINE_MAX_CHARS[cfg.engine])
    if not segments:
        raise ValueError("No text to synthesize")
    logger.info(
        "Rendering %d segments with %s (pause scale %.2f, speed %.2f)",
        len(segments), cfg.engine, cfg.pause_scale, cfg.speed,
    )
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as pool:
        parts = list(pool.map(lambda s: render_segment(s, cfg, tracker), segments))

    pauses = [min(MAX_PAUSE_SECONDS, s.pause_after * cfg.pause_scale) for s in segments]
    voice = audio.loudnorm(audio.join(parts, pauses))

    if jingles:
        intro, outro, voice_delay = pick_jingles()
        episode = audio.mix_episode(voice, intro, outro, voice_delay)
    else:
        episode = voice
    audio.encode_mp3(episode, output_path)
