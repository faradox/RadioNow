# Plan: fine-tuned host voices (dots.tts)

Status: proposal, not started. Written 2026-11-04.

## Why

Zero-shot cloning (Qwen3-TTS, MiniMax) copies a speaker's timbre from one
~10 s clip, but not their delivery: Watts's chuckle and long setups,
McKenna's fast rising spirals, Ram Dass's pauses. Each host has hours of public
lecture audio, which is exactly the data a single-speaker fine-tune needs.
This is the only realistic path toward "indistinguishable".

ElevenLabs is not an option: Professional Voice Clones are own-voice only, and
prominent public figures are on its no-go list.

## Model choice

`dots.tts` (rednote-hilab, June 2026, Apache-2.0, 2B params, 48 kHz):

- Best published speaker similarity on Seed-TTS-Eval (79.2 vs 74.5 for
  Qwen3-TTS 1.7B); ranked first in one (single-listener) blind cloning test.
- Ships training code: `accelerate launch scripts/train_dots_tts.py` with a
  JSONL manifest (`fid`, `audio`, `text`); fine-tune from `dots.tts-soar`.
- Inference: ~8-11 GB VRAM in bf16, RTF ~0.2 on an H800.
- Not hosted on Replicate or fal, so we package it ourselves.

Fallback if dots.tts disappoints: fine-tune Qwen3-TTS 1.7B (same Apache-2.0
licence, already in use zero-shot).

## Steps

1. **Collect** 1-3 h per host of the cleanest available lecture audio
   (studio or radio recordings, not hall recordings with echo and applause).
2. **Prepare** (scripts in `tools/voice_data/`, run locally or on the GPU box):
   - Isolate speech: Demucs/UVR vocal separation, drop music and applause.
   - Transcribe with word timestamps (e.g. Whisper large or
     `gemini-3.5-transcribe`), then split into 3-15 s utterances on pauses.
   - Filter out clips with overlapping speakers, low SNR or bad alignment.
   - Keep disfluencies ("you see", "um", laughs) in the transcripts: they are
     part of the voice.
   - Write `train.jsonl` / `valid.jsonl` (95/5 split).
3. **Train** one fine-tune per host on a rented GPU (RunPod/Lambda, 1x
   A100/H100 80 GB; budget a few hours and roughly $10-30 per host; verify on
   a small run first).
4. **Evaluate** blind: the same 5 script paragraphs per host rendered with
   Qwen3, MiniMax and the fine-tune, plus a real clip; rate without labels.
5. **Serve**: package as a private Replicate model with Cog (weights for the
   three hosts + `speaker` input), cold starts are acceptable for a 6 AM job.
   Add `dotstts` as a third engine in `app/tts.py` (`ENGINES`,
   `render_speech`), with `replicate_model` stored per voice.

## Open questions

- Rights: these are deceased public figures; the estates hold rights to the
  recordings and likeness. Fine for a private toy; revisit before any public
  or commercial use and label the broadcasts as AI-generated.
- Vintage sound: should the output keep the 1960s-70s tape character (more
  "real") or be clean? Decide before data prep, since denoising choices
  depend on it.
