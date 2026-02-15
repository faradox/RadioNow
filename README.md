# Radio Now

![Radio Now screenshot](radionow.png)

AI-powered audio broadcast generator with voice cloning.

## Features

- Create custom voices from short reference audio clips.
- Generate spoken broadcasts from a topic plus optional creative "twist".
- Configure target script length from the admin panel.
- Optionally generate TTS in paragraph-aligned chunks and stitch with silence.
- Auto-generate and store teaser summaries per broadcast.
- Public listing page with inline audio playback and script view.

## Tech Stack

- FastAPI + Jinja2 templates
- MongoDB
- Google Gemini (script + summary generation)
- Replicate qwen3-tts (voice cloning + speech synthesis)
- Docker Compose for local/prod-style deployment

## Quick Start

### 1) Configure environment

Create a local `.env` file (or export vars in your shell):

```bash
RADIONOW_GEMINI_API_KEY=your_gemini_api_key
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
│   ├── main.py
│   ├── generator.py
│   ├── models.py
│   ├── database.py
│   ├── templates/
│   └── static/
├── audio/               # Generated broadcast audio (volume)
├── voices/              # Uploaded voice reference audio (volume)
├── docker-compose.yml
├── Dockerfile
├── Makefile
└── requirements.txt
```

## Notes

- This repo intentionally does not contain runtime secrets.
- `.env` files are gitignored.
- If deploying publicly, place `/admin` behind authentication and HTTPS.
- The stack uses an external Docker network (`breathnet` by default). `make init`/`make run` creates it automatically when missing.
