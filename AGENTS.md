# AGENTS.md

This file provides guidance for AI agents working in this directory.

## Security restrictions

**CRITICAL**:
- Do not access secrets, keys, or passwords.
- Do not execute commands on remote servers.
- Git is read-only (no commits, pushes, or PRs).
- Do not access files owned by root or with restricted permissions.

## Project overview

Radio Now is an AI-powered audio broadcast generator that creates voice-cloned "talks" using:
- Google Gemini for text generation (script writing)
- Replicate qwen3-tts for voice cloning and TTS

Users can create custom voices by uploading reference audio, then generate broadcasts in those voices with custom topics and style transformations.

## Architecture

- **Backend**: FastAPI with Jinja2 templates
- **Database**: MongoDB
- **Storage**: Local filesystem for audio files (mounted volumes)
- **Network**: Docker bridge networking

## Key routes

- `/` - Public broadcast listing
- `/admin` - Admin dashboard (typically placed behind authentication in production)
- `/admin/generate` - Generate new broadcast
- `/admin/voices/*` - Voice management

## Conventions

- Keep files under 500 LOC when possible.
- No emojis in logging, comments, or code.
- Python: Black formatter (88-char lines), async patterns, snake_case
