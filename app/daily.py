"""Daily morning broadcast: headlines -> satirical, comforting host monologue."""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.broadcasts import create_broadcast, engine_for_voice
from app.database import Database, get_voices_collection
from app.generator import OUTPUT_FORMAT, length_requirements, speech_rules
from app.news import fetch_headlines, format_headlines
from app.personas import is_builtin_host, persona_for_voice
from app.tts import engine_supports_cues

logger = logging.getLogger("radionow.daily")

BERLIN = ZoneInfo("Europe/Berlin")
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=15)
STALE_RUN = timedelta(minutes=45)
CATCHUP_WINDOW = timedelta(hours=6)
MIN_HEADLINES = 10

DEFAULT_SETTINGS = {
    "enabled": os.getenv("DAILY_ENABLED", "true").lower() in ("1", "true", "yes"),
    "time": os.getenv("DAILY_TIME", "06:00"),
    "host_voice_ids": [],
    "llm_provider": "",
    "tts_engine": "",
    "target_minutes": 5,
}

_background_tasks: set[asyncio.Task] = set()


def settings_coll():
    return Database.get_db().settings


def runs_coll():
    return Database.get_db().daily_runs


async def get_settings() -> dict:
    stored = await settings_coll().find_one({"_id": "daily"}) or {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k != "_id"}}


async def save_settings(values: dict) -> None:
    await settings_coll().update_one({"_id": "daily"}, {"$set": values}, upsert=True)


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(x) for x in value.split(":", 1))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return 6, 0


async def host_rotation(settings: dict) -> list[dict]:
    """Hosts in rotation order: configured ids, else the built-in hosts."""
    voices = [v async for v in get_voices_collection().find().sort("name", 1)]
    by_id = {str(v["_id"]): v for v in voices}
    hosts = [by_id[i] for i in settings.get("host_voice_ids", []) if i in by_id]
    return hosts or [v for v in voices if is_builtin_host(v)] or voices


def host_for_day(hosts: list[dict], day: date) -> dict:
    return hosts[day.toordinal() % len(hosts)]


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_daily_prompt(
    voice: dict, day: date, wire: str, target_minutes: int, cues_supported: bool
) -> str:
    persona = persona_for_voice(voice) or f"{voice['name']}, a warm and witty host."
    weekday = day.strftime("%A, %B %-d, %Y")
    return f"""You are writing the 6 AM edition of "Radio Now: The Morning \
Broadcast", spoken by one host. Listeners are just waking up, still in bed or \
making coffee. It is {weekday}.

HOST (write entirely in this person's voice and way of thinking; capture the
style, never quote or recycle their famous lines):
{persona}

Below is this morning's raw news wire from several international outlets. It
is source material, not a script.

{wire}

YOUR TASK:
Write the host's morning monologue: a comforting, funny, gently sarcastic
essay that riffs on the day's events. This is satire and philosophy, NOT a
news bulletin.

How to use the wire:
- Find the 2-4 stories that dominate across several outlets; those are the
  day's big themes. Add at most one item from the lighter section if it is
  delightful.
- Never summarise or "read out" the news. Mention each real event in a
  single light sentence at most, just enough for the listener to recognise
  it, then spin it into something new: a parable, a cosmic reinterpretation,
  an absurd "what is really going on here", an imagined scene behind closed
  doors, a mock-serious theory, a conversation with the universe about it.
- Connect the stories into one arc with a single underlying idea, so it
  feels like an essay, not a list of bits.
- Invented material must be unmistakably invented (absurd, playful, framed
  as imagination). Never state made-up facts, quotes or numbers as if real.

Tone and boundaries:
- Wholesome, warm, comedic, a little sarcastic. The listener should finish
  lighter and smiling, never anxious.
- Laugh at the powerful, the pompous, bureaucracy, markets, tech hype,
  self-importance and the news cycle itself. Never at victims, the poor,
  the sick, or ordinary people having a hard day.
- Wars, attacks, disasters, deaths and crimes: do not describe them, give no
  numbers or details, and never joke about suffering. Prefer to leave them
  out. If one dominates every outlet, you may acknowledge it in one tender
  sentence and move on.
- Not partisan. Satire of politicians is fine; telling listeners whom to
  support is not.
- Do not open with "Good morning" plus a weather report, and avoid cliches
  like "buckle up". Open with a hook in the host's own manner.
- End by sending the listener into the day: a small invitation, a practical
  absurdity, a breath, a laugh.

REQUIREMENTS:
{length_requirements(target_minutes)}
{speech_rules(cues_supported)}

{OUTPUT_FORMAT}"""


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def claim_run(day_key: str, force: bool) -> bool:
    """Atomically claim the day's run. False if done/running/exhausted."""
    now = datetime.utcnow()
    if force:
        claimable = [{"status": {"$ne": "running"}}, {"started_at": {"$lt": now - STALE_RUN}}]
    else:
        claimable = [
            {
                "status": "failed",
                "attempts": {"$lt": MAX_ATTEMPTS},
                "finished_at": {"$lt": now - RETRY_AFTER},
            },
            {"status": "running", "started_at": {"$lt": now - STALE_RUN}},
        ]
    try:
        await runs_coll().update_one(
            {"_id": day_key, "$or": claimable},
            {
                "$set": {"status": "running", "started_at": now, "error": None},
                "$inc": {"attempts": 1},
            },
            upsert=True,
        )
        return True
    except DuplicateKeyError:
        return False


async def run_daily(day: date, force: bool = False) -> None:
    """Generate the morning broadcast for `day` (Berlin date)."""
    day_key = day.isoformat()
    if not await claim_run(day_key, force):
        return

    logger.info("Starting daily broadcast for %s (force=%s)", day_key, force)
    try:
        settings = await get_settings()
        hosts = await host_rotation(settings)
        if not hosts:
            raise RuntimeError("No voices available for the daily broadcast")
        voice = host_for_day(hosts, day)

        headlines = await fetch_headlines()
        if len(headlines) < MIN_HEADLINES:
            raise RuntimeError(f"Only {len(headlines)} headlines fetched")

        engine = engine_for_voice(voice, settings.get("tts_engine") or None)
        minutes = int(settings.get("target_minutes") or 5)
        prompt = build_daily_prompt(
            voice, day, format_headlines(headlines), minutes, engine_supports_cues(engine)
        )
        doc = await create_broadcast(
            voice,
            prompt,
            input_text=f"Morning broadcast for {day_key}",
            llm_provider=settings.get("llm_provider") or None,
            tts_engine=engine,
            chunk_minutes=None if engine == "minimax" else 1.0,
            extra_fields={"kind": "daily", "daily_date": day_key},
        )
        await runs_coll().update_one(
            {"_id": day_key},
            {
                "$set": {
                    "status": "done",
                    "finished_at": datetime.utcnow(),
                    "broadcast_id": str(doc["_id"]),
                    "title": doc["title"],
                    "voice_id": str(voice["_id"]),
                    "voice_name": voice["name"],
                    "llm_provider": doc["llm_provider"],
                    "tts_engine": doc["tts_engine"],
                    "headlines": [h.as_dict() for h in headlines],
                }
            },
        )
        logger.info("Daily broadcast for %s done: %s", day_key, doc["title"])
    except Exception as e:
        logger.exception("Daily broadcast for %s failed", day_key)
        await runs_coll().update_one(
            {"_id": day_key},
            {"$set": {"status": "failed", "finished_at": datetime.utcnow(), "error": str(e)}},
        )


def start_run_in_background(day: date, force: bool = True) -> None:
    task = asyncio.create_task(run_daily(day, force=force))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def scheduler_loop() -> None:
    """Check every minute whether today's broadcast is due (Berlin time)."""
    await asyncio.sleep(15)
    while True:
        try:
            settings = await get_settings()
            if settings["enabled"]:
                now = datetime.now(BERLIN)
                hour, minute = parse_time(settings["time"])
                due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if due <= now < due + CATCHUP_WINDOW:
                    await run_daily(now.date())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily scheduler tick failed")
        await asyncio.sleep(60)


async def recent_runs(limit: int = 14) -> list[dict]:
    return [r async for r in runs_coll().find().sort("_id", -1).limit(limit)]


def today_berlin() -> date:
    return datetime.now(BERLIN).date()


def valid_object_ids(values: list[str]) -> list[str]:
    return [v for v in values if ObjectId.is_valid(v)]
