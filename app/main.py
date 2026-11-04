import asyncio
import logging
import mimetypes
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from bson import ObjectId
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slugify import slugify

from app import daily
from app.broadcasts import create_broadcast, engine_for_voice
from app.database import Database, get_broadcasts_collection, get_voices_collection
from app.generator import (
    build_script_prompt,
    delete_broadcast_audio,
    delete_voice_audio,
    get_audio_duration,
    save_voice_audio,
)
from app.llm import available_providers, default_provider
from app.personas import builtin_pacing_for, builtin_persona_for, persona_for_voice
from app.tts import ENGINES, engine_supports_cues, resolve_engine

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("radionow")

BASE_DIR = Path(__file__).parent
AUDIO_DIR = Path("/app/audio")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await Database.connect()
    await daily.prepare_runs()
    scheduler = asyncio.create_task(daily.scheduler_loop())
    yield
    scheduler.cancel()
    with suppress(asyncio.CancelledError):
        await scheduler
    await Database.disconnect()


app = FastAPI(title="Radio Now", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def format_duration(seconds) -> str:
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60}:{total % 60:02d}"


def audio_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "audio/mpeg"


templates.env.globals["format_duration"] = format_duration
templates.env.globals["audio_mime"] = audio_mime


def redirect(tab: str, message: str | None = None, error: str | None = None):
    url = f"/admin?tab={tab}"
    if message:
        url += f"&message={quote(message)}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


def derive_summary(doc: dict) -> str:
    summary = (doc.get("summary") or "").strip()
    if summary:
        return summary
    topic, twist = (doc.get("topic") or "").strip(), (doc.get("twist") or "").strip()
    if topic:
        return f"{topic} {twist}".strip()
    text = " ".join((doc.get("input_text") or "").split())
    return f"{text[:217].rstrip()}..." if len(text) > 220 else text


async def ensure_duration_seconds(coll, doc: dict) -> None:
    """Backfill duration from the audio file when the stored value is missing."""
    try:
        seconds = int(round(float(doc.get("duration_seconds") or 0)))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0 and doc.get("audio_filename"):
        path = AUDIO_DIR / doc["audio_filename"]
        if path.exists():
            seconds = get_audio_duration(path)
            if seconds > 0:
                await coll.update_one({"_id": doc["_id"]}, {"$set": {"duration_seconds": seconds}})
    doc["duration_seconds"] = seconds


async def load_broadcasts(limit: int = 0) -> list[dict]:
    coll = get_broadcasts_collection()
    names = {str(v["_id"]): v["name"] async for v in get_voices_collection().find()}
    out = []
    async for doc in coll.find().sort("created_at", -1).limit(limit):
        await ensure_duration_seconds(coll, doc)
        doc["voice_name"] = names.get(doc.get("voice_id"), "Unknown")
        doc["summary_display"] = derive_summary(doc)
        doc["_id"] = str(doc["_id"])
        out.append(doc)
    return out


# =============================================================================
# Public routes
# =============================================================================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    broadcasts = await load_broadcasts()
    return templates.TemplateResponse(request, "index.html", {"broadcasts": broadcasts})


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    file_path = (AUDIO_DIR / filename).resolve()
    if file_path.parent != AUDIO_DIR.resolve() or not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(file_path, media_type=audio_mime(filename), filename=filename)


@app.get("/health")
async def health():
    return {"status": "ok"}


# =============================================================================
# Admin
# =============================================================================


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, tab: str = "generate"):
    voices = []
    async for doc in get_voices_collection().find().sort("created_at", -1):
        doc["_id"] = str(doc["_id"])
        doc["engine"] = engine_for_voice(doc)
        doc["builtin_persona"] = builtin_persona_for(doc["name"]) or ""
        doc["default_pause_scale"], doc["default_speed"] = builtin_pacing_for(doc["name"])
        voices.append(doc)

    context = {
        "tab": tab,
        "voices": voices,
        "engines": ENGINES,
        "providers": available_providers(),
        "default_provider": default_provider(),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }
    if tab == "generate":
        recent = await load_broadcasts(limit=30)
        context["recent_broadcasts"] = recent
        context["recent_total_usd"] = sum(
            (b.get("cost") or {}).get("total_usd", 0.0) for b in recent
        )
    if tab == "daily":
        settings = await daily.get_settings()
        hosts = await daily.host_rotation(settings)
        context.update(
            settings=settings,
            rotation=[{"_id": str(h["_id"]), "name": h["name"]} for h in hosts],
            today_host=daily.host_for_day(hosts, daily.today_berlin())["name"] if hosts else None,
            runs=await daily.recent_runs(),
            default_stance=daily.DEFAULT_STANCE,
        )
    return templates.TemplateResponse(request, "admin.html", context)


@app.post("/admin/generate")
async def generate_broadcast(
    voice_id: str = Form(...),
    input_text: str = Form(...),
    twist: str = Form(None),
    style_override: str = Form(None),
    target_minutes: int = Form(4),
    jingles: str | None = Form(None),
    llm_provider: str = Form(""),
    tts_engine: str = Form(""),
):
    try:
        voice = await get_voices_collection().find_one({"_id": ObjectId(voice_id)})
        if not voice:
            raise ValueError("Voice not found")
        twist_text = (twist or "").strip() or None
        style_text = (style_override or "").strip() or None
        engine = engine_for_voice(voice, tts_engine or None)

        prompt = build_script_prompt(
            input_text=input_text,
            twist=twist_text,
            target_minutes=min(max(int(target_minutes), 1), 20),
            persona=persona_for_voice(voice),
            cues_supported=engine_supports_cues(engine),
        )
        doc = await create_broadcast(
            voice,
            prompt,
            input_text=input_text,
            llm_provider=llm_provider or None,
            tts_engine=engine,
            style_override=style_text,
            jingles=jingles is not None,
            extra_fields={"twist": twist_text, "target_minutes": target_minutes},
        )
        return redirect("generate", message=f"Broadcast created: {doc['title']}")
    except Exception as e:
        logger.exception("Failed to generate broadcast")
        return redirect("generate", error=f"Generation failed: {e}")


@app.post("/admin/broadcasts/{broadcast_id}/delete")
async def delete_broadcast(broadcast_id: str):
    coll = get_broadcasts_collection()
    try:
        broadcast = await coll.find_one({"_id": ObjectId(broadcast_id)})
        if not broadcast:
            raise ValueError("Broadcast not found")
        if broadcast.get("audio_filename"):
            delete_broadcast_audio(broadcast["audio_filename"])
        await coll.delete_one({"_id": broadcast["_id"]})
        return redirect("generate", message=f"Broadcast deleted: {broadcast['title']}")
    except Exception as e:
        logger.exception("Failed to delete broadcast")
        return redirect("generate", error=f"Failed to delete broadcast: {e}")


# --- Voices ------------------------------------------------------------------


def parse_optional_float(value: str, low: float, high: float) -> float | None:
    """Blank means "use the default"; otherwise clamp to [low, high]."""
    try:
        return min(max(float(value), low), high) if value.strip() else None
    except ValueError:
        return None


@app.post("/admin/voices/create")
async def create_voice(
    name: str = Form(...),
    style_instruction: str = Form(...),
    reference_text: str = Form(...),
    persona: str = Form(""),
    tts_engine: str = Form(""),
    reference_audio: UploadFile = File(...),
):
    coll = get_voices_collection()
    try:
        name, style_instruction = name.strip(), style_instruction.strip()
        reference_text = reference_text.strip()
        if not name:
            raise ValueError("Name is required")
        if len(style_instruction) < 10 or len(reference_text) < 10:
            raise ValueError("Style instruction and reference text need 10+ characters")
        slug = slugify(name)
        if not slug:
            raise ValueError("Invalid name for slug generation")
        if await coll.find_one({"slug": slug}):
            raise ValueError(f"A voice with slug '{slug}' already exists")
        audio_data = await reference_audio.read()
        if len(audio_data) < 1000:
            raise ValueError("Audio file is too small")

        await coll.insert_one(
            {
                "name": name,
                "slug": slug,
                "style_instruction": style_instruction,
                "reference_text": reference_text,
                "persona": persona.strip(),
                "tts_engine": resolve_engine(tts_engine),
                "audio_filename": save_voice_audio(audio_data, slug),
                "created_at": datetime.utcnow(),
            }
        )
        return redirect("voices", message=f"Voice created: {name}")
    except Exception as e:
        logger.exception("Failed to create voice")
        return redirect("voices", error=f"Failed to create voice: {e}")


@app.post("/admin/voices/{voice_id}/update")
async def update_voice(
    voice_id: str,
    style_instruction: str = Form(...),
    reference_text: str = Form(...),
    persona: str = Form(""),
    tts_engine: str = Form(""),
    pause_scale: str = Form(""),
    speech_speed: str = Form(""),
    reference_audio: UploadFile | None = File(None),
):
    coll = get_voices_collection()
    try:
        voice = await coll.find_one({"_id": ObjectId(voice_id)})
        if not voice:
            raise ValueError("Voice not found")
        updates = {
            "style_instruction": style_instruction.strip(),
            "reference_text": reference_text.strip(),
            "persona": persona.strip(),
            "tts_engine": resolve_engine(tts_engine),
            "pause_scale": parse_optional_float(pause_scale, 0.5, 3.0),
            "speech_speed": parse_optional_float(speech_speed, 0.7, 1.3),
        }
        unset = {}
        if reference_audio is not None and reference_audio.filename:
            audio_data = await reference_audio.read()
            if len(audio_data) < 1000:
                raise ValueError("Audio file is too small")
            updates["audio_filename"] = save_voice_audio(audio_data, voice["slug"])
            delete_voice_audio(voice["audio_filename"])
            unset["minimax_voice_id"] = ""  # re-clone from the new reference
        change = {"$set": updates, **({"$unset": unset} if unset else {})}
        await coll.update_one({"_id": voice["_id"]}, change)
        return redirect("voices", message=f"Voice updated: {voice['name']}")
    except Exception as e:
        logger.exception("Failed to update voice")
        return redirect("voices", error=f"Failed to update voice: {e}")


@app.post("/admin/voices/{voice_id}/delete")
async def delete_voice(voice_id: str):
    coll = get_voices_collection()
    try:
        voice = await coll.find_one({"_id": ObjectId(voice_id)})
        if not voice:
            raise ValueError("Voice not found")
        count = await get_broadcasts_collection().count_documents({"voice_id": voice_id})
        if count:
            raise ValueError(f"{count} broadcasts use this voice")
        delete_voice_audio(voice["audio_filename"])
        await coll.delete_one({"_id": voice["_id"]})
        return redirect("voices", message=f"Voice deleted: {voice['name']}")
    except Exception as e:
        logger.exception("Failed to delete voice")
        return redirect("voices", error=f"Failed to delete voice: {e}")


# --- Daily broadcast -----------------------------------------------------------


@app.post("/admin/daily/settings")
async def update_daily_settings(request: Request):
    form = await request.form()
    hour, minute = daily.parse_time(str(form.get("time", "06:00")))
    await daily.save_settings(
        {
            "enabled": form.get("enabled") is not None,
            "time": f"{hour:02d}:{minute:02d}",
            "host_voice_ids": daily.valid_object_ids(
                [str(v) for v in form.getlist("host_voice_ids")]
            ),
            "llm_provider": str(form.get("llm_provider", "")),
            "tts_engine": str(form.get("tts_engine", "")),
            "target_minutes": min(max(int(form.get("target_minutes") or 5), 2), 15),
            "jingles": form.get("jingles") is not None,
            # Saving the untouched default keeps following future default updates.
            "stance": (
                ""
                if str(form.get("stance", "")).strip() == daily.DEFAULT_STANCE.strip()
                else str(form.get("stance", "")).strip()
            ),
        }
    )
    return redirect("daily", message="Daily broadcast settings saved")


@app.post("/admin/daily/run")
async def run_daily_now():
    daily.start_run_in_background(daily.today_berlin(), trigger="manual")
    return redirect(
        "daily", message="Today's broadcast started. Reload in a few minutes."
    )


@app.post("/admin/daily/runs/{run_id}/retry")
async def retry_daily_run(run_id: str):
    # A retry always generates for today, from this morning's headlines.
    daily.start_run_in_background(daily.today_berlin(), trigger="retry", retry_of=run_id)
    return redirect("daily", message="Trying again. Reload in a few minutes.")
