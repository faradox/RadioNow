"""Mono float32 PCM helpers: decode, pacing, loudness, mixing, MP3 encoding.

All functions are blocking (numpy + ffmpeg subprocesses).
"""

import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
FRAME_SECONDS = 0.01
RAW_FMT = ["-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE)]


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:300]}")
    return result.stdout


def _from_raw(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def decode(audio_bytes: bytes) -> np.ndarray:
    """Decode any container/codec to mono float32 at SAMPLE_RATE."""
    samples = _from_raw(_ffmpeg(["-i", "pipe:0", *RAW_FMT, "pipe:1"], audio_bytes))
    if not samples.size:
        raise ValueError("Decoded audio is empty")
    return samples


def decode_file(path: Path) -> np.ndarray:
    return decode(path.read_bytes())


def apply_filter(samples: np.ndarray, audio_filter: str) -> np.ndarray:
    raw = samples.astype("<f4").tobytes()
    return _from_raw(
        _ffmpeg([*RAW_FMT, "-i", "pipe:0", "-af", audio_filter, *RAW_FMT, "pipe:1"], raw)
    )


def change_tempo(samples: np.ndarray, speed: float) -> np.ndarray:
    """Pitch-preserving tempo change (speed < 1 slows down)."""
    if abs(speed - 1.0) < 0.01:
        return samples
    return apply_filter(samples, f"atempo={min(max(speed, 0.5), 2.0):.3f}")


def loudnorm(samples: np.ndarray) -> np.ndarray:
    """Normalize speech to podcast loudness (-16 LUFS, -2 dBTP)."""
    return apply_filter(samples, "loudnorm=I=-16:TP=-2:LRA=11")


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(max(0.0, seconds) * SAMPLE_RATE), dtype=np.float32)


# ---------------------------------------------------------------------------
# Silence analysis and pacing
# ---------------------------------------------------------------------------


def frame_db(samples: np.ndarray) -> tuple[np.ndarray, int]:
    """Per-frame RMS level in dBFS and the frame length in samples."""
    n = int(SAMPLE_RATE * FRAME_SECONDS)
    frames = len(samples) // n
    if frames == 0:
        return np.array([]), n
    blocks = samples[: frames * n].reshape(frames, n)
    rms = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms), n


def silent_frames(db: np.ndarray) -> np.ndarray:
    """Frames that hold no speech, only silence or background noise.

    Adapts to the take: on clean audio silence is 30 dB under the speech; on
    vintage-sounding takes (hiss, crackle only ~25 dB under the speech) frames
    within 10 dB of the noise floor count as silence.
    """
    if not db.size:
        return np.array([], dtype=bool)
    speech_level = float(np.percentile(db, 90))
    noise_floor = float(np.percentile(db, 3))
    threshold = min(speech_level - 20.0, max(noise_floor + 10.0, speech_level - 30.0))
    return db < max(-60.0, threshold)


def _fade(samples: np.ndarray, fade_in: float, fade_out: float) -> np.ndarray:
    out = samples.copy()
    n_in, n_out = int(fade_in * SAMPLE_RATE), int(fade_out * SAMPLE_RATE)
    if 0 < n_in < len(out):
        out[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if 0 < n_out < len(out):
        out[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return out


def trim(samples: np.ndarray, lead: float = 0.03, tail: float = 0.12) -> np.ndarray:
    """Remove leading/trailing silence, keeping a little air and soft edges."""
    db, n = frame_db(samples)
    loud = np.flatnonzero(~silent_frames(db))
    if not loud.size:
        return samples
    start = max(0, loud[0] * n - int(lead * SAMPLE_RATE))
    end = min(len(samples), (loud[-1] + 1) * n + int(tail * SAMPLE_RATE))
    return _fade(samples[start:end], 0.005, 0.03)


def stretch_gaps(
    samples: np.ndarray,
    scale: float,
    min_gap: float = 0.12,
    max_extra: float = 0.7,
) -> np.ndarray:
    """Lengthen the natural pauses inside speech (commas, sentence ends)."""
    if scale <= 1.01:
        return samples
    db, n = frame_db(samples)
    mask = silent_frames(db)
    pieces, last, i = [], 0, 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < len(mask) and mask[j]:
            j += 1
        gap = (j - i) * FRAME_SECONDS
        if gap >= min_gap and i > 0 and j < len(mask):
            middle = ((i + j) // 2) * n
            pieces.append(samples[last:middle])
            pieces.append(silence(min(max_extra, gap * (scale - 1.0))))
            last = middle
        i = j
    pieces.append(samples[last:])
    return np.concatenate(pieces)


def join(parts: list[np.ndarray], pauses: list[float]) -> np.ndarray:
    """Concatenate parts with pauses[i] seconds of silence after parts[i]."""
    out = []
    for idx, part in enumerate(parts):
        out.append(part)
        if idx < len(parts) - 1:
            out.append(silence(pauses[idx]))
    return np.concatenate(out) if out else silence(0)


# ---------------------------------------------------------------------------
# Room tone: keep a voice's background texture (tape hiss, crackle) going
# through the pauses we insert, so breaks don't drop into digital silence.
# ---------------------------------------------------------------------------

DIGITAL_SILENCE_DB = -80.0
MIN_ROOM_TONE_SECONDS = 0.25


def extract_room_tone(samples: np.ndarray) -> np.ndarray:
    """Collect the background-only frames of a raw (untrimmed) take."""
    db, n = frame_db(samples)
    if not db.size:
        return silence(0)
    quiet = silent_frames(db) & (db > DIGITAL_SILENCE_DB)
    # Skip frames right next to speech (breaths, word tails).
    padded = np.convolve((~quiet).astype(int), np.ones(7, dtype=int), mode="same") == 0
    frames = np.flatnonzero(quiet & padded)
    if not frames.size:
        return silence(0)
    return np.concatenate([samples[i * n : (i + 1) * n] for i in frames])


def room_tone_bed(tone: np.ndarray, length: int, seed: int = 0) -> np.ndarray:
    """A seamless bed of `length` samples from random grains of `tone`.

    Hann-windowed grains at 50% overlap sum to unity gain, so the texture
    keeps the source level without audible loop points.
    """
    grain = min(len(tone), int(0.4 * SAMPLE_RATE))
    if grain < int(0.05 * SAMPLE_RATE) or length <= 0:
        return np.zeros(max(0, length), dtype=np.float32)
    hop = grain // 2
    window = np.hanning(grain + 1)[:-1].astype(np.float32)  # periodic Hann
    rng = np.random.default_rng(seed)
    bed = np.zeros(length + grain, dtype=np.float32)
    for start in range(0, length, hop):
        offset = int(rng.integers(0, len(tone) - grain + 1))
        bed[start : start + grain] += tone[offset : offset + grain] * window
    return bed[:length]


def fill_silence(samples: np.ndarray, tone: np.ndarray, ramp: float = 0.03) -> np.ndarray:
    """Lay room tone wherever the track is digitally silent, with soft ramps."""
    if len(tone) < MIN_ROOM_TONE_SECONDS * SAMPLE_RATE:
        return samples
    db, n = frame_db(samples)
    gate = (db < DIGITAL_SILENCE_DB).astype(np.float32)
    width = max(1, int(ramp / FRAME_SECONDS))
    gate = np.convolve(gate, np.ones(width, dtype=np.float32) / width, mode="same")
    gate = np.repeat(gate, n)
    gate = np.pad(gate, (0, max(0, len(samples) - len(gate))), mode="edge")[: len(samples)]
    return samples + room_tone_bed(tone, len(samples)) * gate


# ---------------------------------------------------------------------------
# Mixing
# ---------------------------------------------------------------------------


def _place(track: np.ndarray, clip: np.ndarray, start: int) -> None:
    track[start : start + len(clip)] += clip


def voice_activity(voice: np.ndarray, smooth_seconds: float = 0.6) -> np.ndarray:
    """0..1 per-sample envelope of where speech is present (smoothed)."""
    db, n = frame_db(voice)
    active = (~silent_frames(db)).astype(np.float32)
    width = max(1, int(smooth_seconds / FRAME_SECONDS))
    kernel = np.ones(width, dtype=np.float32) / width
    smoothed = np.clip(np.convolve(active, kernel, mode="same") * 2.0, 0.0, 1.0)
    env = np.repeat(smoothed, n)
    return np.pad(env, (0, max(0, len(voice) - len(env))), mode="edge")[: len(voice)]


def mix_episode(
    voice: np.ndarray,
    intro: list[tuple[float, np.ndarray]],
    outro: list[tuple[float, np.ndarray]],
    voice_delay: float,
    outro_gap: float = 1.4,
    duck_db: float = -14.0,
) -> np.ndarray:
    """Lay voice over intro bowls; strike outro bowls after the last word.

    intro/outro are (start offset in seconds, clip). Outro offsets are relative
    to the end of speech plus outro_gap. Bowls are ducked under the voice and
    every clip plays to its natural end.
    """
    sr = SAMPLE_RATE
    voice_start = int(voice_delay * sr)
    voice_end = voice_start + len(voice)
    outro_start = voice_end + int(outro_gap * sr)

    placements = [(int(off * sr), clip) for off, clip in intro]
    placements += [(outro_start + int(off * sr), clip) for off, clip in outro]
    total = max([voice_end] + [start + len(clip) for start, clip in placements])

    bowls = np.zeros(total, dtype=np.float32)
    for start, clip in placements:
        _place(bowls, clip, start)

    duck = np.ones(total, dtype=np.float32)
    depth = 1.0 - 10 ** (duck_db / 20)
    duck[voice_start:voice_end] -= depth * voice_activity(voice)

    out = bowls * duck
    _place(out, voice, voice_start)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.97:
        out *= 0.97 / peak
    return out


def encode_mp3_bytes(samples: np.ndarray, bitrate: str = "192k") -> bytes:
    return _ffmpeg(
        [*RAW_FMT, "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3",
         "pipe:1"],
        samples.astype("<f4").tobytes(),
    )


def encode_mp3(samples: np.ndarray, output_path: Path, bitrate: str = "192k") -> None:
    _ffmpeg(
        [*RAW_FMT, "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", bitrate, "-y",
         str(output_path)],
        samples.astype("<f4").tobytes(),
    )
