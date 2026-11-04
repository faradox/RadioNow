# Radio Now

![Radio Now screenshot](radionow.png)

AI-powered audio broadcast generator with voice cloning.

## Features

- Create custom voices from short reference audio clips, each with a writing
  persona (built-in for Alan Watts, Terence McKenna, Ram Dass) and a TTS engine.
- Generate spoken broadcasts from a topic plus optional creative "twist".
- Switch the script LLM (Gemini 4 Argon or Claude Opus 5.5) and the TTS engine
  (Qwen3-TTS or MiniMax Speech HD) per broadcast to compare them.
- Daily morning broadcast: at 06:00 Europe/Berlin the app pulls world headlines
  from public RSS feeds and the day's host (rotating) turns them into a
  comforting, satirical monologue. Settings and run history under
  `/admin?tab=daily`.
- Real timing: scripts mark beats with [pause] / [long pause]; each piece is
  rendered separately and joined with exact silences, natural gaps are
  stretched per voice (pause scale, speech speed in the Voices tab).
- Singing bowl intro/outro: synthesized per episode (random pentatonic bowls,
  rendered until fully silent), or recordings dropped into `jingles/`.
- Per-episode cost (LLM tokens + Replicate usage) in the admin panel; prices in
  `app/costs.py`, Replicate rates overridable via `RADIONOW_REPLICATE_PRICES`.
- Daily editorial stance editable in the Daily tab.
- Configure target script length from the admin panel.
- Auto-generate and store teaser summaries per broadcast.
- Public listing page with inline audio playback and script view.

## Tech Stack

- FastAPI + Jinja2 templates
- MongoDB
- Google Gemini (`google-genai`, default `gemini-4-argon`) or Anthropic Claude
  (`anthropic`, default `claude-opus-5-5`) for script + summary generation
- Replicate `qwen/qwen3-tts` or `minimax/speech-2.8-hd` (with
  `minimax/voice-cloning`) for speech; ffmpeg stitches chunks and writes a
  loudness-normalized MP3
- In-process scheduler (asyncio, Europe/Berlin) for the daily broadcast
- Docker Compose for local/prod-style deployment

## Quick Start

### 1) Configure environment

Create a local `.env` file (or export vars in your shell):

```bash
RADIONOW_GEMINI_API_KEY=your_gemini_api_key        # at least one LLM key
RADIONOW_ANTHROPIC_API_KEY=your_anthropic_api_key
RADIONOW_REPLICATE_API_TOKEN=your_replicate_api_token
RADIONOW_PORT=8090
# Optional: if set, radionow uses this external/shared MongoDB and does NOT
# start the local mongodb service.
# RADIONOW_MONGO_URL=mongodb://user:pass@host:27017/

# Optional local Mongo credentials (used when RADIONOW_MONGO_URL is unset):
# RADIONOW_MONGO_INITDB_ROOT_USERNAME=admin
# RADIONOW_MONGO_INITDB_ROOT_PASSWORD=admin

# Optional docker network override (defaults to breathnet):
# DOCKER_NETWORK_NAME=breathnet
```

### 2) Build and run

```bash
make run
```

Behavior:
- If `RADIONOW_MONGO_URL` is **unset**, `make run` starts a local MongoDB container (`localdb` profile).
- If `RADIONOW_MONGO_URL` is **set**, `make run` starts only the app and uses that external MongoDB URL.

### 3) Open the app

- Public page: `http://localhost:8090/`
- Admin panel: `http://localhost:8090/admin`

## Makefile Commands

These match the repository `Makefile`:

- `make build` - Build the Docker image
- `make init` - Ensure `audio/` and `voices/` directories exist
- `make run` - Start services
- `make logs` - Show application logs
- `make shell` - Open a shell in the app container
- `make stop` - Stop services
- `make help` - Show command help

## Project Structure

```text
radionow/
├── app/
│   ├── main.py          # routes
│   ├── broadcasts.py    # script -> speech -> stored broadcast pipeline
│   ├── generator.py     # prompts, response parsing, file helpers
│   ├── llm.py           # Gemini / Claude providers
│   ├── tts.py           # Replicate TTS engines, segmenting, episode rendering
│   ├── audio.py         # numpy/ffmpeg PCM: pacing, loudness, mixing, MP3
│   ├── bowls.py         # singing bowl synthesis / jingle recordings
│   ├── costs.py         # LLM + Replicate pricing, per-episode cost tracking
│   ├── daily.py         # morning broadcast prompt, runs, scheduler
│   ├── news.py          # RSS/Atom headline fetching
│   ├── personas.py      # built-in host writing personas
│   ├── models.py
│   ├── database.py
│   ├── templates/
│   └── static/
├── audio/               # Generated broadcast audio (volume)
├── voices/              # Uploaded voice reference audio (volume)
├── jingles/             # Optional singing bowl recordings (volume, read-only)
├── docker-compose.yml
├── Dockerfile
├── Makefile
└── requirements.txt
```

## Notes

- See `.env.sample` for all optional settings (models, daily time, feeds).
- The scheduler runs inside the app process, so keep uvicorn at one worker.
  Runs are claimed atomically in the `daily_runs` collection (one per Berlin
  date), so a restart never produces a duplicate episode.
- Plan for fine-tuned, much more lifelike host voices: `docs/voice-finetuning-plan.md`.

- This repo intentionally does not contain runtime secrets.
- `.env` files are gitignored.
- If deploying publicly, place `/admin` behind authentication and HTTPS.
- The stack uses an external Docker network (`breathnet` by default). `make init`/`make run` creates it automatically when missing.
