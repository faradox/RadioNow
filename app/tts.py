"""Text-to-speech engines (Replicate) and episode rendering.

The script is split into segments at the LLM's [pause] / [long pause] markers,
paragraph breaks and (for long passages) sentence boundaries. Each segment is
synthesized separately, trimmed, and its natural pauses stretched; segments
are then joined with exact silences. This gives the host real timing instead
of one breathless read. All functions here are blocking.
"""

import io
import logging
import os
import random
import re
import threading
import time
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


class _CreateGate:
    """Shared pacing for prediction creates across all render threads.

    Replicate throttles creates (e.g. 6/min, burst 1, on accounts with under
    $5 credit). After a 429 every thread waits out the reported reset, and
    creates stay spaced for a while so the run finishes instead of failing.
    """

    SPACED_FOR = 600.0  # seconds to keep spacing creates after a throttle
    SPACING = 10.5  # seconds between creates while throttled (6/min)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._not_before = 0.0
        self._spaced_until = 0.0

    def wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._not_before)
            if now < self._spaced_until:
                self._not_before = start + self.SPACING
            delay = start - now
        if delay > 0:
            time.sleep(delay)

    def throttled(self, wait: float) -> None:
        with self._lock:
            now = time.monotonic()
            self._not_before = max(self._not_before, now + wait)
            self._spaced_until = now + self.SPACED_FOR


_gate = _CreateGate()
MAX_THROTTLE_RETRIES = 12


def _is_throttle(error: Exception) -> bool:
    text = str(error).lower()
    return getattr(error, "status", None) == 429 or "429" in text or "throttled" in text


def _throttle_wait(error: Exception) -> float:
    match = re.search(r"resets in ~?(\d+(?:\.\d+)?)\s*s", str(error))
    return (float(match.group(1)) if match else 10.0) + random.uniform(0.5, 2.0)


def _create_prediction(model: str, inputs: dict, **params):
    for attempt in range(MAX_THROTTLE_RETRIES + 1):
        _gate.wait_turn()
        try:
            return replicate.models.predictions.create(
                model=model, input=inputs, **params
            )
        except Exception as e:
            if not _is_throttle(e) or attempt == MAX_THROTTLE_RETRIES:
                raise
            wait = _throttle_wait(e)
            logger.warning("Replicate throttled %s; waiting %.0fs", model, wait)
            _gate.throttled(wait)
            for value in inputs.values():  # re-upload files from the start
                if hasattr(value, "seek"):
                    value.seek(0)


def run_prediction(model: str, inputs: dict, **params):
    """Run a Replicate prediction; return (output, predict_time seconds)."""
    prediction = _create_prediction(model, inputs, **params)
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


# MiniMax accepts mp3/m4a/wav, 10 s to 5 min, under 20 MB, and checks that the
# extension matches the content. Stored references are always named .wav
# whatever was uploaded, so they are re-encoded to a real MP3 first.
CLONE_MIN_SECONDS = 10.0
CLONE_MAX_SECONDS = 280.0


def clone_reference_mp3(voice_audio_path: Path) -> bytes:
    """The reference as a genuine mono MP3, trimmed to MiniMax's limits."""
    samples = audio.decode_file(voice_audio_path)
    seconds = len(samples) / audio.SAMPLE_RATE
    if seconds < CLONE_MIN_SECONDS:
        raise ValueError(
            f"Reference audio is {seconds:.1f} s; MiniMax cloning needs at least "
            f"{CLONE_MIN_SECONDS:.0f} s"
        )
    samples = samples[: int(CLONE_MAX_SECONDS * audio.SAMPLE_RATE)]
    return audio.encode_mp3_bytes(samples, bitrate="160k")


def clone_minimax_voice(voice_audio_path: Path, tracker: CostTracker) -> str:
    """Create a MiniMax cloned voice from the reference audio; return voice_id."""
    mp3 = io.BytesIO(clone_reference_mp3(voice_audio_path))
    mp3.name = "reference.mp3"  # the upload's filename and content type
    inputs = {
        "voice_file": mp3,
        "model": MINIMAX_CLONE_TARGET,
        "need_noise_reduction": True,
        "need_volume_normalization": True,
    }
    try:
        output, predict_time = run_prediction(MINIMAX_CLONE_MODEL, inputs)
    except RuntimeError as e:
        if "file ext" not in str(e):
            raise
        # Fall back to an inline data URL (data:audio/mpeg), which carries
        # the type explicitly instead of relying on the uploaded file's URL.
        logger.warning("MiniMax rejected the uploaded file (%s); retrying inline", e)
        mp3.seek(0)
        output, predict_time = run_prediction(
            MINIMAX_CLONE_MODEL, inputs, file_encoding_strategy="base64"
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


def render_segment(
    seg: Segment, cfg: VoiceRender, tracker: CostTracker
) -> tuple[np.ndarray, np.ndarray]:
    """Return (paced segment audio, room tone sampled from the raw take)."""
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
    paced = audio.stretch_gaps(audio.trim(samples), cfg.pause_scale)
    return paced, audio.extract_room_tone(samples)


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
        rendered = list(pool.map(lambda s: render_segment(s, cfg, tracker), segments))
    parts = [part for part, _ in rendered]
    room_tone = np.concatenate([tone for _, tone in rendered])

    pauses = [min(MAX_PAUSE_SECONDS, s.pause_after * cfg.pause_scale) for s in segments]
    # Fill inserted silences with the take's own background before loudnorm,
    # so breaks keep the recording's texture instead of going dead.
    voice = audio.fill_silence(audio.join(parts, pauses), room_tone)
    logger.info("Room tone: %.1fs sampled", len(room_tone) / audio.SAMPLE_RATE)
    voice = audio.loudnorm(voice)

    if jingles:
        intro, outro, voice_delay = pick_jingles()
        episode = audio.mix_episode(voice, intro, outro, voice_delay)
    else:
        episode = voice
    audio.encode_mp3(episode, output_path)
