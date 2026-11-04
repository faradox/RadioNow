"""Daily morning broadcast: headlines -> satirical, comforting host monologue."""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.broadcasts import create_broadcast, engine_for_voice
from app.database import Database, get_broadcasts_collection, get_voices_collection
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

DEFAULT_STANCE = """\
The show sees through the spectacle, and the host speaks from that place:
- Politics is theatre. Elections, parties, summits and scandals are a stage
  play for the audience; the costumes and slogans change, the script and the
  owners of the theatre do not. Left and right are two teams in the same
  show, and the listener is invited to stop rooting for either.
- The news is not information but programming. It manufactures "the current
  thing": what to fear this week, whom to hate, what to argue about. It is
  shaped by intelligence agencies, PR firms and a handful of media owners,
  and "official consensus" is part of the costume.
- Real power sits with a small, unaccountable club of the ultra-rich and
  their institutions (central banks, arms makers, tech monopolies, the
  permanent bureaucracy) that needs the 99% distracted, anxious and divided.
- Read every headline as a scene in that play: ask what it is distracting
  from, who profits when the audience feels this way, and what the stage
  managers would rather nobody noticed.
- The host's mission is to wake the listener up gently: to break the spell
  with laughter, hand them back their attention (the one thing the system
  cannot run without) and remind them that the real world is the one in
  front of their nose, which is still astonishing."""

DEFAULT_SETTINGS = {
    "enabled": os.getenv("DAILY_ENABLED", "true").lower() in ("1", "true", "yes"),
    "time": os.getenv("DAILY_TIME", "06:00"),
    "host_voice_ids": [],
    "llm_provider": "",
    "tts_engine": "",
    "target_minutes": 5,
    "jingles": True,
    "stance": "",
}

_background_tasks: set[asyncio.Task] = set()


def settings_coll():
    return Database.get_db().settings


def runs_coll():
    """One document per run (manual, retry or scheduled)."""
    return Database.get_db().daily_runs


def days_coll():
    """One document per Berlin date: the scheduler's claim and retry state."""
    return Database.get_db().daily_schedule


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
    voice: dict,
    day: date,
    wire: str,
    target_minutes: int,
    cues_supported: bool,
    stance: str | None = None,
) -> str:
    persona = persona_for_voice(voice) or f"{voice['name']}, a warm and witty host."
    weekday = day.strftime("%A, %B %-d, %Y")
    stance = (stance or "").strip() or DEFAULT_STANCE
    return f"""You are writing the 6 AM edition of "Radio Now: The Morning \
Broadcast", spoken by one host. Listeners are just waking up, still in bed or \
making coffee. It is {weekday}.

HOST (write entirely in this person's voice and way of thinking; capture the
style, never quote or recycle their famous lines):
{persona}

EDITORIAL STANCE (the lens of the whole show; let it cut through every
segment, in the host's own idiom, with wit rather than ranting):
{stance}

Below is this morning's raw news wire from several mainstream outlets. It is
source material, i.e. the official script of today's play, not the truth.

{wire}

YOUR TASK:
Write the host's morning monologue: a piercing, funny, comforting essay that
riffs on the day's events through the stance above. This is satire and
philosophy, NOT a news bulletin.

The mood (most important): the stance is the lens, delight is the mood.
- The host is not angry about the show, he is enormously amused by it. Seeing
  through the puppet theatre is the good news: once you see the strings, the
  whole production becomes hilarious. Aim for at least two laughs for every
  sharp observation, and never let more than a paragraph pass without one.
- Zen paradox: the harder the powerful try to control everything, the more
  ridiculous they look; the more seriously the news takes itself, the
  funnier it gets; the cure for the spell is not to fight it but to laugh.
- "Silly me": the host regularly catches himself getting sucked into the
  drama ("and for a moment there I was terribly worried about the bond
  markets, as if I owned any bonds!") and laughs at himself. He is a fellow
  audience member who keeps wandering off to the snack bar, not a prophet.
- Cosmic perspective: from far enough out, even the villains are actors the
  universe hired to play villains, rather badly, in a show it puts on for its
  own amusement. Wonder and mischief over doom.
- No bitterness, no despair, no lists of grievances. If a passage turns
  heavy, end it with a turn toward the absurd, then a [pause].

How to use the wire:
- Find the 2-4 stories that dominate across several outlets; those are
  today's "current thing". Add at most one item from the lighter section if
  it is delightful.
- Never summarise or "read out" the news. Mention each real event in a single
  light sentence at most, just enough for the listener to recognise it, then
  pull back the curtain: what is the show really about, who is in the
  audience, who sells the tickets? Use parables, mock-cosmic interpretations,
  imagined scenes backstage, mock-serious theories.
- Connect the stories into one arc with a single underlying idea, so it
  feels like an essay, not a list of bits.
- Be pointed, and deliver every point as a joke. Name the trick, mock the
  stagecraft, and do not soften the stance with "of course both sides have a
  point" or appeals to trust institutions.

Boundaries (they keep the satire sharp and honest):
- Present the stance as the host's perspective and satire. Do not invent
  specific facts (fake quotes, documents, numbers or secret operations) and
  state them as real; invented material must be unmistakably playful.
- Mock institutions, roles, public behaviour and power. Do not accuse named
  real people of specific crimes.
- Never laugh at victims, the poor, the sick or ordinary people. Wars,
  attacks, disasters, deaths and crimes: no descriptions, no numbers, no
  jokes about suffering. Leave them out or acknowledge them in one tender
  sentence, which may note who profits from the fear.
- Do not open with "Good morning" plus a weather report, and avoid cliches
  like "buckle up". Open with a hook in the host's own manner.
- End by sending the listener into the day free, awake and amused rather
  than angry or paranoid: a small invitation, a practical absurdity, a
  breath, a laugh.

REQUIREMENTS:
{length_requirements(target_minutes)}
{speech_rules(cues_supported)}

{OUTPUT_FORMAT}"""


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def claim_scheduled_day(day_key: str) -> bool:
    """Atomically claim the scheduler's slot for a day.

    False if the day is done, a scheduled attempt is running, or retries are
    exhausted / not yet due. Manual runs and retries never claim this slot.
    """
    now = datetime.utcnow()
    claimable = [
        {
            "status": "failed",
            "attempts": {"$lt": MAX_ATTEMPTS},
            "finished_at": {"$lt": now - RETRY_AFTER},
        },
        {"status": "running", "started_at": {"$lt": now - STALE_RUN}},
    ]
    try:
        await days_coll().update_one(
            {"_id": day_key, "$or": claimable},
            {"$set": {"status": "running", "started_at": now}, "$inc": {"attempts": 1}},
            upsert=True,
        )
        return True
    except DuplicateKeyError:
        return False


async def run_daily(
    day: date, trigger: str = "schedule", retry_of: str | None = None
) -> None:
    """Generate one morning broadcast for `day` (Berlin date).

    Every run writes its own document, so runs never overwrite each other.
    """
    day_key = day.isoformat()
    if trigger == "schedule" and not await claim_scheduled_day(day_key):
        return

    inserted = await runs_coll().insert_one(
        {
            "day": day_key,
            "trigger": trigger,
            "retry_of": retry_of,
            "status": "running",
            "started_at": datetime.utcnow(),
        }
    )
    run_id = inserted.inserted_id
    logger.info("Starting daily run %s for %s (%s)", run_id, day_key, trigger)
    try:
        settings = await get_settings()
        hosts = await host_rotation(settings)
        if not hosts:
            raise RuntimeError("No voices available for the daily broadcast")
        voice = host_for_day(hosts, day)
        await runs_coll().update_one(
            {"_id": run_id},
            {"$set": {"voice_id": str(voice["_id"]), "voice_name": voice["name"]}},
        )

        headlines = await fetch_headlines()
        if len(headlines) < MIN_HEADLINES:
            raise RuntimeError(f"Only {len(headlines)} headlines fetched")

        engine = engine_for_voice(voice, settings.get("tts_engine") or None)
        minutes = int(settings.get("target_minutes") or 5)
        prompt = build_daily_prompt(
            voice,
            day,
            format_headlines(headlines),
            minutes,
            engine_supports_cues(engine),
            settings.get("stance"),
        )
        doc = await create_broadcast(
            voice,
            prompt,
            input_text=f"Morning broadcast for {day_key}",
            llm_provider=settings.get("llm_provider") or None,
            tts_engine=engine,
            jingles=bool(settings.get("jingles", True)),
            extra_fields={
                "kind": "daily",
                "daily_date": day_key,
                "daily_run_id": str(run_id),
            },
        )
        finished = datetime.utcnow()
        await runs_coll().update_one(
            {"_id": run_id},
            {
                "$set": {
                    "status": "done",
                    "finished_at": finished,
                    "broadcast_id": str(doc["_id"]),
                    "title": doc["title"],
                    "llm_provider": doc["llm_provider"],
                    "tts_engine": doc["tts_engine"],
                    "cost": doc["cost"],
                    "headlines": [h.as_dict() for h in headlines],
                }
            },
        )
        # Any successful run covers the day, so the scheduler won't add another.
        await days_coll().update_one(
            {"_id": day_key},
            {"$set": {"status": "done", "finished_at": finished, "run_id": run_id}},
            upsert=True,
        )
        logger.info("Daily run %s done: %s", run_id, doc["title"])
    except Exception as e:
        logger.exception("Daily run %s for %s failed", run_id, day_key)
        finished = datetime.utcnow()
        await runs_coll().update_one(
            {"_id": run_id},
            {"$set": {"status": "failed", "finished_at": finished, "error": str(e)}},
        )
        if trigger == "schedule":
            await days_coll().update_one(
                {"_id": day_key},
                {"$set": {"status": "failed", "finished_at": finished}},
            )


def start_run_in_background(
    day: date, trigger: str = "manual", retry_of: str | None = None
) -> None:
    task = asyncio.create_task(run_daily(day, trigger=trigger, retry_of=retry_of))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


LEGACY_SUCCESS_FIELDS = (
    "broadcast_id", "title", "voice_id", "voice_name",
    "llm_provider", "tts_engine", "cost", "headlines",
)


async def prepare_runs() -> None:
    """Startup housekeeping: split legacy per-day records, fail orphaned runs.

    Before 2026-11-04 one record per day was reused by every run, so a failed
    manual run could overwrite the day's earlier success. Those records are
    split back into a success run and a failed run.
    """
    async for legacy in runs_coll().find({"day": {"$exists": False}}):
        day_key = str(legacy["_id"])
        base = {"day": day_key, "retry_of": None}
        if legacy.get("broadcast_id"):
            broadcast = None
            if ObjectId.is_valid(legacy["broadcast_id"]):
                broadcast = await get_broadcasts_collection().find_one(
                    {"_id": ObjectId(legacy["broadcast_id"])}, {"created_at": 1}
                )
            when = (broadcast or {}).get("created_at") or legacy.get("started_at")
            await runs_coll().insert_one(
                {
                    **base,
                    **{k: legacy.get(k) for k in LEGACY_SUCCESS_FIELDS},
                    "trigger": "schedule",
                    "status": "done",
                    "started_at": when,
                    "finished_at": when,
                }
            )
        if legacy.get("status") == "failed" or legacy.get("error"):
            await runs_coll().insert_one(
                {
                    **base,
                    "trigger": "manual",
                    "status": "failed",
                    "error": legacy.get("error") or "unknown error",
                    "started_at": legacy.get("started_at"),
                    "finished_at": legacy.get("finished_at"),
                }
            )
        await days_coll().update_one(
            {"_id": day_key},
            {
                "$setOnInsert": {
                    "status": "done" if legacy.get("broadcast_id") else legacy.get("status"),
                    "attempts": legacy.get("attempts", 1),
                    "finished_at": legacy.get("finished_at"),
                }
            },
            upsert=True,
        )
        await runs_coll().delete_one({"_id": legacy["_id"]})
        logger.info("Split legacy daily run record %s", day_key)

    # Runs live in this process; anything still "running" at startup is dead.
    result = await runs_coll().update_many(
        {"status": "running"},
        {
            "$set": {
                "status": "failed",
                "finished_at": datetime.utcnow(),
                "error": "Interrupted (app restarted during the run)",
            }
        },
    )
    if result.modified_count:
        logger.warning("Marked %d interrupted daily runs as failed", result.modified_count)


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


async def recent_runs(limit: int = 20) -> list[dict]:
    cursor = runs_coll().find({"day": {"$exists": True}}).sort("started_at", -1)
    runs = []
    async for run in cursor.limit(limit):
        run["_id"] = str(run["_id"])
        runs.append(run)
    return runs


def today_berlin() -> date:
    return datetime.now(BERLIN).date()


def valid_object_ids(values: list[str]) -> list[str]:
    return [v for v in values if ObjectId.is_valid(v)]
