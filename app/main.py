import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from bson import ObjectId
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slugify import slugify

from app.database import (
    Database,
    get_broadcasts_collection,
    get_voices_collection,
)
from app.generator import (
    delete_broadcast_audio,
    delete_voice_audio,
    generate_audio,
    generate_audio_chunked,
    generate_script,
    get_audio_duration,
    get_voice_audio_path,
    save_voice_audio,
)
from app.models import GenerationResult

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("radionow")

# Paths
BASE_DIR = Path(__file__).parent
AUDIO_DIR = Path("/app/audio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    await Database.connect()
    yield
    await Database.disconnect()


app = FastAPI(title="Radio Now", lifespan=lifespan)

# Static files and templates
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# Helper to format duration
def format_duration(seconds: int | float | str | None) -> str:
    """Format seconds as M:SS."""
    try:
        total_seconds = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        total_seconds = 0
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes}:{secs:02d}"


# Add template globals
templates.env.globals["format_duration"] = format_duration


def derive_summary(doc: dict) -> str:
    """Get a display-ready summary for a broadcast."""
    summary = (doc.get("summary") or "").strip()
    if summary:
        return summary

    topic = (doc.get("topic") or "").strip()
    twist = (doc.get("twist") or "").strip()
    if topic and twist:
        return f"{topic} {twist}"
    if topic:
        return topic

    input_text = " ".join((doc.get("input_text") or "").split())
    if len(input_text) > 220:
        return f"{input_text[:217].rstrip()}..."
    return input_text


async def ensure_duration_seconds(
    broadcasts_coll,
    doc: dict,
) -> None:
    """Backfill duration from audio file when database value is missing/zero."""
    existing_value = doc.get("duration_seconds")
    try:
        existing_seconds = int(round(float(existing_value or 0)))
    except (TypeError, ValueError):
        existing_seconds = 0

    if existing_seconds > 0:
        doc["duration_seconds"] = existing_seconds
        return

    audio_filename = doc.get("audio_filename")
    if not audio_filename:
        doc["duration_seconds"] = 0
        return

    audio_path = AUDIO_DIR / audio_filename
    if not audio_path.exists():
        doc["duration_seconds"] = 0
        return

    computed_seconds = get_audio_duration(audio_path)
    doc["duration_seconds"] = computed_seconds
    if computed_seconds > 0:
        await broadcasts_coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {"duration_seconds": computed_seconds}},
        )


# =============================================================================
# Public Routes
# =============================================================================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Public broadcast listing page."""
    broadcasts_coll = get_broadcasts_collection()
    voices_coll = get_voices_collection()

    # Get all broadcasts, newest first
    broadcasts = []
    cursor = broadcasts_coll.find().sort("created_at", -1)
    async for doc in cursor:
        await ensure_duration_seconds(broadcasts_coll, doc)

        # Get voice name
        voice = await voices_coll.find_one({"_id": ObjectId(doc["voice_id"])})
        doc["voice_name"] = voice["name"] if voice else "Unknown"
        doc["summary_display"] = derive_summary(doc)
        doc["_id"] = str(doc["_id"])
        broadcasts.append(doc)

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "broadcasts": broadcasts},
    )


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    """Serve generated audio files."""
    file_path = AUDIO_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(
        file_path,
        media_type="audio/wav",
        filename=filename,
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


# =============================================================================
# Admin Routes
# =============================================================================


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, tab: str = "generate"):
    """Admin dashboard with generate and voices tabs."""
    voices_coll = get_voices_collection()
    broadcasts_coll = get_broadcasts_collection()

    # Get all voices
    voices = []
    cursor = voices_coll.find().sort("created_at", -1)
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        voices.append(doc)

    # Get recent broadcasts for the generate tab
    recent_broadcasts = []
    cursor = broadcasts_coll.find().sort("created_at", -1).limit(5)
    async for doc in cursor:
        await ensure_duration_seconds(broadcasts_coll, doc)
        voice = await voices_coll.find_one({"_id": ObjectId(doc["voice_id"])})
        doc["voice_name"] = voice["name"] if voice else "Unknown"
        doc["_id"] = str(doc["_id"])
        recent_broadcasts.append(doc)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "tab": tab,
            "voices": voices,
            "recent_broadcasts": recent_broadcasts,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/admin/generate")
async def generate_broadcast(
    request: Request,
    voice_id: str = Form(...),
    input_text: str = Form(...),
    twist: str = Form(None),
    style_override: str = Form(None),
    target_minutes: int = Form(4),
    generate_in_chunks: str | None = Form(None),
    chunk_minutes: float = Form(1.0),
):
    """Generate a new broadcast."""
    voices_coll = get_voices_collection()
    broadcasts_coll = get_broadcasts_collection()

    try:
        # Get the voice
        voice = await voices_coll.find_one({"_id": ObjectId(voice_id)})
        if not voice:
            raise HTTPException(status_code=404, detail="Voice not found")

        # Twist is used for Gemini text generation
        twist_text = twist.strip() if twist and twist.strip() else None

        # Style instruction is used for TTS - use override if provided
        style_instruction = (
            style_override.strip()
            if style_override and style_override.strip()
            else voice.get("style_instruction")
        )
        bounded_target_minutes = min(max(int(target_minutes), 1), 20)
        bounded_chunk_minutes = min(max(float(chunk_minutes), 0.25), 10.0)
        chunking_enabled = generate_in_chunks is not None

        logger.info("Generating broadcast with voice: %s", voice["name"])

        # Generate script using Gemini (twist affects content/style of text)
        title, script, summary = await generate_script(
            input_text=input_text,
            twist=twist_text,
            target_minutes=bounded_target_minutes,
        )

        # Generate audio using TTS (style_instruction affects speech delivery)
        voice_audio_path = get_voice_audio_path(voice["audio_filename"])
        if chunking_enabled:
            audio_path, duration_seconds = await generate_audio_chunked(
                script=script,
                voice_audio_path=voice_audio_path,
                reference_text=voice["reference_text"],
                style_instruction=style_instruction,
                chunk_minutes=bounded_chunk_minutes,
            )
        else:
            audio_path, duration_seconds = await generate_audio(
                script=script,
                voice_audio_path=voice_audio_path,
                reference_text=voice["reference_text"],
                style_instruction=style_instruction,
            )

        # Extract topic from input (first 50 chars)
        topic = input_text[:50].strip()
        if len(input_text) > 50:
            topic += "..."

        # Save broadcast to database
        broadcast_doc = {
            "title": title,
            "voice_id": voice_id,
            "topic": topic,
            "twist": twist_text,
            "style_override": style_override.strip() if style_override and style_override.strip() else None,
            "input_text": input_text,
            "generated_script": script,
            "summary": summary,
            "audio_filename": audio_path.name,
            "duration_seconds": duration_seconds,
            "target_minutes": bounded_target_minutes,
            "generate_in_chunks": chunking_enabled,
            "chunk_minutes": bounded_chunk_minutes,
            "created_at": datetime.utcnow(),
        }
        result = await broadcasts_coll.insert_one(broadcast_doc)

        logger.info("Created broadcast: %s (%s)", title, result.inserted_id)

        return RedirectResponse(
            url=f"/admin?tab=generate&message=Broadcast created: {title}",
            status_code=303,
        )

    except Exception as e:
        logger.error("Failed to generate broadcast: %s", e)
        return RedirectResponse(
            url=f"/admin?tab=generate&error=Generation failed: {str(e)}",
            status_code=303,
        )


@app.post("/admin/voices/create")
async def create_voice(
    request: Request,
    name: str = Form(...),
    style_instruction: str = Form(...),
    reference_text: str = Form(...),
    reference_audio: UploadFile = File(...),
):
    """Create a new voice."""
    voices_coll = get_voices_collection()

    try:
        # Validate inputs
        name = name.strip()
        if not name:
            raise ValueError("Name is required")

        style_instruction = style_instruction.strip()
        if len(style_instruction) < 10:
            raise ValueError("Style instruction must be at least 10 characters")

        reference_text = reference_text.strip()
        if len(reference_text) < 10:
            raise ValueError("Reference text must be at least 10 characters")

        # Generate slug
        slug = slugify(name)
        if not slug:
            raise ValueError("Invalid name for slug generation")

        # Check for duplicate slug
        existing = await voices_coll.find_one({"slug": slug})
        if existing:
            raise ValueError(f"A voice with slug '{slug}' already exists")

        # Validate and save audio file
        if not reference_audio.filename:
            raise ValueError("Reference audio file is required")

        audio_data = await reference_audio.read()
        if len(audio_data) < 1000:
            raise ValueError("Audio file is too small")

        audio_filename = save_voice_audio(audio_data, slug)

        # Create voice document
        voice_doc = {
            "name": name,
            "slug": slug,
            "style_instruction": style_instruction,
            "reference_text": reference_text,
            "audio_filename": audio_filename,
            "created_at": datetime.utcnow(),
        }
        result = await voices_coll.insert_one(voice_doc)

        logger.info("Created voice: %s (%s)", name, result.inserted_id)

        return RedirectResponse(
            url=f"/admin?tab=voices&message=Voice created: {name}",
            status_code=303,
        )

    except Exception as e:
        logger.error("Failed to create voice: %s", e)
        return RedirectResponse(
            url=f"/admin?tab=voices&error=Failed to create voice: {str(e)}",
            status_code=303,
        )


@app.post("/admin/voices/{voice_id}/delete")
async def delete_voice(voice_id: str):
    """Delete a voice."""
    voices_coll = get_voices_collection()
    broadcasts_coll = get_broadcasts_collection()

    try:
        # Check if voice exists
        voice = await voices_coll.find_one({"_id": ObjectId(voice_id)})
        if not voice:
            raise HTTPException(status_code=404, detail="Voice not found")

        # Check if voice is used by any broadcasts
        broadcast_count = await broadcasts_coll.count_documents({"voice_id": voice_id})
        if broadcast_count > 0:
            return RedirectResponse(
                url=f"/admin?tab=voices&error=Cannot delete voice: {broadcast_count} broadcasts use this voice",
                status_code=303,
            )

        # Delete audio file
        delete_voice_audio(voice["audio_filename"])

        # Delete from database
        await voices_coll.delete_one({"_id": ObjectId(voice_id)})

        logger.info("Deleted voice: %s", voice["name"])

        return RedirectResponse(
            url=f"/admin?tab=voices&message=Voice deleted: {voice['name']}",
            status_code=303,
        )

    except Exception as e:
        logger.error("Failed to delete voice: %s", e)
        return RedirectResponse(
            url=f"/admin?tab=voices&error=Failed to delete voice: {str(e)}",
            status_code=303,
        )


@app.post("/admin/broadcasts/{broadcast_id}/delete")
async def delete_broadcast(broadcast_id: str):
    """Delete a broadcast and its audio file."""
    broadcasts_coll = get_broadcasts_collection()

    try:
        # Check if broadcast exists
        broadcast = await broadcasts_coll.find_one({"_id": ObjectId(broadcast_id)})
        if not broadcast:
            raise HTTPException(status_code=404, detail="Broadcast not found")

        # Delete audio file
        if broadcast.get("audio_filename"):
            delete_broadcast_audio(broadcast["audio_filename"])

        # Delete from database
        await broadcasts_coll.delete_one({"_id": ObjectId(broadcast_id)})

        logger.info("Deleted broadcast: %s", broadcast["title"])

        return RedirectResponse(
            url=f"/admin?tab=generate&message=Broadcast deleted: {broadcast['title']}",
            status_code=303,
        )

    except Exception as e:
        logger.error("Failed to delete broadcast: %s", e)
        return RedirectResponse(
            url=f"/admin?tab=generate&error=Failed to delete broadcast: {str(e)}",
            status_code=303,
        )
