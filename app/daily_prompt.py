"""Prompt for the daily morning broadcast: stance, host angle, form of the day."""

import random
from datetime import date

from app.generator import OUTPUT_FORMAT, length_requirements, speech_rules
from app.personas import morning_angle_for_voice, persona_for_voice

# Stated plainly on purpose: the imagery has to come from each host, not from
# the stance (an image-heavy stance made every host reuse the same metaphors).
DEFAULT_STANCE = """\
The show sees through the spectacle, and the host speaks from that place:
- Party politics is a spectacle for the audience. Elections, summits and
  scandals change the faces and slogans, not who holds power. Left and right
  are two teams in the same game, and the listener is invited to stop
  rooting for either.
- The news is less information than programming. It manufactures "the
  current thing": what to fear this week, whom to hate, what to argue about.
  It is shaped by intelligence agencies, PR firms and a handful of media
  owners, and "official consensus" is part of the packaging.
- Real power sits with a small, unaccountable club of the ultra-rich and
  their institutions (central banks, arms makers, tech monopolies, the
  permanent bureaucracy) that needs the 99% distracted, anxious and divided.
- Read every headline with these questions: what is it distracting from,
  who profits when the audience feels this way, and what would the people
  in charge rather nobody noticed?
- The host's mission is to wake the listener up gently: to break the spell
  with laughter, hand them back their attention (the one thing the system
  cannot run without) and remind them that the real world is the one in
  front of their nose, which is still astonishing."""

# Rotated by date so episodes differ in shape, not only in topic.
FORMS = [
    "A single extended parable or fable that today's stories turn out to fit"
    " into, told the way this host tells stories.",
    "A mock lecture or seminar on today's \"current thing\", the host as an"
    " increasingly digressive professor.",
    "An open letter: to the listener, to the news itself, or to one of the"
    " institutions in today's stories.",
    "Field notes of an outside observer (an anthropologist, a visitor from"
    " far away, a historian a thousand years from now) on this week's"
    " strange human rituals.",
    "A morning walk: start from something small and physical (the kettle, the"
    " window, a bird, the cold floor) and let the stories surface along the"
    " way.",
    "A review of today's news as if it were some other genre (a nature"
    " documentary, a sports commentary, a cookery programme, a weather"
    " report for the soul), in the host's own manner.",
    "Imaginary listener questions about today's stories, answered one by one"
    " in the host's own way.",
    "A story from the host's own life or experience that today's events keep"
    " echoing.",
]


def form_for_day(day: date, voice_name: str) -> str:
    return random.Random(f"{day.isoformat()}:{voice_name}").choice(FORMS)


def recent_section(recent: list[dict]) -> str:
    """List recent episodes so the host avoids repeating them."""
    if not recent:
        return ""
    lines = []
    for ep in recent:
        motifs = ", ".join(ep.get("motifs") or [])
        line = f'- {ep.get("voice_name", "?")}: "{ep.get("title", "")}"'
        line += f" (images: {motifs})" if motifs else f' ({ep.get("summary", "")})'
        lines.append(line)
    listed = "\n".join(lines)
    return f"""
RECENT EPISODES (the listener heard these; do not reuse their titles, central
images, metaphors, running jokes or overall framing; find fresh ones):
{listed}
"""


def build_daily_prompt(
    voice: dict,
    day: date,
    wire: str,
    target_minutes: int,
    cues_supported: bool,
    stance: str | None = None,
    recent: list[dict] | None = None,
) -> str:
    persona = persona_for_voice(voice) or f"{voice['name']}, a warm and witty host."
    angle = morning_angle_for_voice(voice) or (
        "- Reads the news through their own persona above: their own themes,"
        " material and kind of humour."
    )
    weekday = day.strftime("%A, %B %-d, %Y")
    stance = (stance or "").strip() or DEFAULT_STANCE
    return f"""You are writing the 6 AM edition of "Radio Now: The Morning \
Broadcast", spoken by one host. Listeners are just waking up, still in bed or \
making coffee. It is {weekday}.

HOST (write entirely in this person's voice and way of thinking; capture the
style, never quote or recycle their famous lines):
{persona}

HOW THIS HOST READS THE NEWS:
{angle}

EDITORIAL STANCE (the shared premise of the station, in plain words):
{stance}

Fidelity comes first. The stance is the premise; the host translates it into
their own worldview, vocabulary, topics and images, the way this person
really thought and talked. Do not echo the stance's wording. Avoid the stock
images any writer would reach for (puppets and strings, theatre and stage,
costumes, steering wheels, the Matrix, sheep); if a picture fits every host,
it is the wrong picture. Where the stance and the host's real views differ,
the host argues it their own way.

Below is this morning's raw news wire from several mainstream outlets. It is
source material, i.e. the official version of the day, not the truth.

{wire}
{recent_section(recent or [])}
YOUR TASK:
Write the host's morning monologue: a pointed, funny, comforting essay that
riffs on the day's events through the stance above. This is satire and
philosophy, NOT a news bulletin.

Today's form (adapt it to the host, or loosen it if it fights their style):
{form_for_day(day, voice.get("name", ""))}

The mood: the stance is the lens, delight is the mood.
- Seeing through the spectacle is the good news: the host is amused by it,
  not angry. Keep the laughs coming, in this host's own kind of humour (see
  above), and never let a passage turn heavy without a turn toward the
  absurd, then a [pause].
- Let the host catch themself getting sucked into the drama now and then,
  and laugh at that, in their own way: a fellow audience member, not a
  prophet.
- From far enough out, the whole production is the universe amusing itself.
  Wonder and mischief over doom. No bitterness, no despair, no lists of
  grievances.

How to use the wire:
- Find the 2-4 stories that dominate across several outlets; those are
  today's "current thing". Add at most one item from the lighter section if
  it is delightful.
- Never summarise or "read out" the news. Mention each real event in a single
  light sentence at most, just enough for the listener to recognise it, then
  show what is going on underneath, through the host's own lens.
- Connect the stories into one arc with a single underlying idea, so it
  feels like an essay, not a list of bits.
- Be pointed, and deliver every point with humour. Do not soften the stance
  with "of course both sides have a point" or appeals to trust institutions.

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
  than angry or paranoid, in the way this host would.

REQUIREMENTS:
{length_requirements(target_minutes)}
{speech_rules(cues_supported)}

{OUTPUT_FORMAT}"""
