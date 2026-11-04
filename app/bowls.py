"""Singing bowl jingles: synthesized bowls or recordings dropped into jingles/.

Synthesis model: a struck bowl rings in a few inharmonic modes. Each mode is
a slightly detuned pair (the bowl is not perfectly round), which produces the
characteristic slow beating. Higher modes decay faster. Every bowl is rendered
until it has decayed below -75 dB, so nothing is ever cut off.
"""

import logging
import random
from pathlib import Path

import numpy as np

from app.audio import SAMPLE_RATE, decode_file, frame_db

logger = logging.getLogger("radionow.bowls")

JINGLES_DIR = Path("/app/jingles")
JINGLE_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".aif", ".aiff"}

# (frequency ratio to fundamental, relative amplitude, relative decay time).
# Ratios follow measured Himalayan bowl modes (roughly 1 : 2.7 : 5.2 : 8.6).
BOWL_MODES = [
    (1.000, 1.00, 1.00),
    (2.740, 0.55, 0.55),
    (5.270, 0.26, 0.33),
    (8.630, 0.12, 0.21),
    (12.70, 0.05, 0.14),
]
# F major pentatonic fundamentals (Hz) so overlapping bowls never clash.
FUNDAMENTALS = [174.6, 196.0, 220.0, 261.6, 293.7, 349.2, 392.0]
DECAY_FLOOR_DB = 75.0
PEAK_LEVEL = 0.42  # about -7.5 dBFS, sits under -16 LUFS speech


def synth_bowl(rng: np.random.Generator, f0: float | None = None) -> np.ndarray:
    """Render one struck bowl to silence, normalized to PEAK_LEVEL."""
    sr = SAMPLE_RATE
    f0 = f0 or float(rng.choice(FUNDAMENTALS)) * rng.uniform(0.985, 1.015)
    t60 = rng.uniform(14.0, 22.0)  # seconds for the fundamental to fall 60 dB
    duration = t60 * DECAY_FLOOR_DB / 60.0
    t = np.arange(int(duration * sr), dtype=np.float64) / sr
    out = np.zeros_like(t)

    for ratio, amp, decay in BOWL_MODES:
        freq = f0 * ratio * rng.uniform(0.997, 1.003)
        if freq > 15000:
            continue
        mode_t60 = t60 * decay
        envelope = np.exp(-np.log(1000.0) * t / mode_t60)  # 60 dB per mode_t60
        beat_hz = rng.uniform(0.3, 0.9) * (1 + 0.35 * (ratio - 1))
        balance = rng.uniform(0.55, 0.7)  # unequal pair: wobble, not tremolo
        a = amp * rng.uniform(0.8, 1.2)
        for offset, weight in ((-beat_hz / 2, balance), (beat_hz / 2, 1 - balance)):
            phase = rng.uniform(0, 2 * np.pi)
            out += a * weight * envelope * np.sin(2 * np.pi * (freq + offset) * t + phase)

    # Mallet contact: a very short, soft noise thump.
    n_strike = int(0.03 * sr)
    noise = rng.standard_normal(n_strike) * np.exp(-np.arange(n_strike) / (0.004 * sr))
    noise = np.convolve(noise, np.ones(12) / 12, mode="same")  # take the hiss off
    out[:n_strike] += 0.05 * noise

    # Soft attack (felt mallet) and a final fade over the last, inaudible bit.
    attack = int(0.004 * sr)
    out[:attack] *= np.linspace(0, 1, attack)
    tail = int(0.5 * sr)
    out[-tail:] *= np.linspace(1, 0, tail)

    out *= PEAK_LEVEL / max(1e-9, float(np.max(np.abs(out))))
    return out.astype(np.float32)


def load_recordings() -> list[Path]:
    if not JINGLES_DIR.is_dir():
        return []
    return sorted(p for p in JINGLES_DIR.iterdir() if p.suffix.lower() in JINGLE_SUFFIXES)


def prepare_recording(path: Path) -> np.ndarray:
    """Load a recording, normalize it, and fade it out if it ends abruptly."""
    samples = decode_file(path)
    peak = float(np.max(np.abs(samples))) or 1.0
    samples = samples * (PEAK_LEVEL / peak)
    db, _ = frame_db(samples[-int(0.3 * SAMPLE_RATE) :])
    if db.size and float(np.max(db)) > -50.0:
        logger.warning("Jingle %s ends abruptly; applying a fade-out", path.name)
        fade = min(len(samples), int(2.5 * SAMPLE_RATE))
        samples[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32) ** 2
    return samples.astype(np.float32)


def pick_jingles(seed: int | None = None):
    """Return (intro, outro) lists of (offset seconds, clip) and a voice delay."""
    rng = np.random.default_rng(seed)
    choose = random.Random(seed)
    recordings = load_recordings()

    def clip() -> np.ndarray:
        if recordings:
            return prepare_recording(choose.choice(recordings))
        return synth_bowl(rng)

    intro_count = choose.choice([1, 2, 2, 3])
    outro_count = choose.choice([1, 2, 3])
    intro, offset = [], 0.0
    for _ in range(intro_count):
        intro.append((offset, clip()))
        offset += rng.uniform(1.6, 3.2)
    voice_delay = intro[-1][0] + rng.uniform(3.0, 4.5)

    outro, offset = [], 0.0
    for _ in range(outro_count):
        outro.append((offset, clip()))
        offset += rng.uniform(2.0, 3.5)
    return intro, outro, voice_delay
