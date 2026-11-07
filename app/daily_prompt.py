"""Prompt for the daily broadcast: stance, host angle, form of the day."""

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
- The host's mission is to break the spell with laughter and hand the
  listener back their attention, the one thing the system cannot run
  without, and with it a world far stranger and better than the headlines."""

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
    "A wander: start from something small and concrete, far from the news,"
    " and let the stories surface along the way.",
    "A review of today's news as if it were some other genre (a nature"
    " documentary, a sports commentary, a cookery programme, a weather"
    " report), in the host's own manner.",
    "Imaginary listener questions about today's stories, answered one by one"
    " in the host's own way.",
    "A story from the host's own life or experience that today's events keep"
    " echoing.",
]


def form_for_day(day: date, voice_name: str, rotation_size: int = 1) -> str:
    """Each host walks through all forms in a host-specific order.

    The host's n-th appearance in the rotation gets the n-th form of their
    permutation, so a host never repeats a form within len(FORMS) episodes.
    """
    order = random.Random(voice_name).sample(range(len(FORMS)), len(FORMS))
    appearance = day.toordinal() // max(1, rotation_size)
    return FORMS[order[appearance % len(FORMS)]]


def recent_section(recent: list[dict]) -> str:
    """List recent episodes so the host avoids repeating them."""
    if not recent:
        return ""
    lines = []
    for ep in recent:
        motifs = ", ".join(ep.get("motifs") or [])
        line = f'- {ep.get("voice_name", "?")}: "{ep.get("title", "")}"'
        line += f" (images: {motifs})" if motifs else f' ({ep.get("summary", "")})'
        if ep.get("opening"):
            line += f'\n  Opened with: "{ep["opening"]}"'
        lines.append(line)
    listed = "\n".join(lines)
    return f"""
RECENT EPISODES (the listener heard these; do not reuse their titles, central
images, metaphors, running jokes, overall framing or the shape and wording of
their openings; find fresh ones):
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
    rotation_size: int = 1,
) -> str:
    persona = persona_for_voice(voice) or f"{voice['name']}, a warm and witty host."
    angle = morning_angle_for_voice(voice) or (
        "- Reads the news through their own persona above: their own themes,"
        " material and kind of humour."
    )
    weekday = day.strftime("%A, %B %-d, %Y")
    stance = (stance or "").strip() or DEFAULT_STANCE
    return f"""You are writing today's episode of "Radio Now", a daily broadcast \
in which one host riffs on the news. It is {weekday}.

HOST (write entirely in this person's voice and way of thinking; capture the
style, never quote or recycle their famous lines):
{persona}

HOW THIS HOST READS THE NEWS:
{angle}

EDITORIAL STANCE (the shared premise of the station, in plain words):
{stance}

Fidelity comes first: the host translates the stance into their own
worldview, vocabulary and images, the way this person really thought and
talked, and argues it their own way where their views differ. Do not echo
the stance's wording or reach for stock images (puppets, stages, costumes,
the Matrix, sheep); if a picture would fit every host, it is the wrong one.

TODAY'S NEWS WIRE (several mainstream outlets; the official version of the
day, not the truth):

{wire}
{recent_section(recent or [])}
YOUR TASK:
A monologue that is pointed, funny and in the end comforting: satire and
philosophy on the day's events, not a news bulletin.

Today's form (adapt or loosen it if it fights the host's style):
{form_for_day(day, voice.get("name", ""), rotation_size)}

Guidance:
- Pick the stories that dominate across outlets, plus at most one lighter
  item if it delights. Never read out or summarise the news: a light
  sentence per event, then what is going on underneath. Tie it together
  with one underlying idea.
- The host is amused rather than angry; the humour is their own kind.
  Pointed, without "both sides" hedging or appeals to trust institutions.
- The host's material above is a pool, not a checklist: draw on one or
  two things, different ones from recent episodes, or on something new
  that fits them.
- Surprise the listener. Every episode should feel like it could only have
  been written today, by this host; avoid the obvious arc and ending.
- The episode is not about the time of day or the listener's routine; they
  may be listening at any hour. Leave waking up, breakfast and hot drinks
  out unless today's news is about them.

Boundaries:
- The stance is the host's perspective and satire. Do not present invented
  specifics (quotes, documents, numbers, secret operations) as real;
  invented material must be unmistakably playful.
- Mock institutions, roles, public behaviour and power; do not accuse named
  real people of specific crimes.
- Never laugh at victims, the poor, the sick or ordinary people. Wars,
  disasters, deaths and crimes: no descriptions, no numbers, no jokes; leave
  them out or acknowledge them in one tender sentence.
- No greeting formulas, no self-introductions, no opening shaped like a
  recent episode's. Leave the listener lighter and more awake, not angry or
  paranoid.

REQUIREMENTS:
{length_requirements(target_minutes)}
{speech_rules(cues_supported)}

{OUTPUT_FORMAT}"""
