"""Built-in writing personas for the radio hosts.

A persona describes how a host *writes and talks* (rhetoric, rhythm, humour,
recurring themes). It feeds the script LLM, not the TTS engine. Voices can
override it with their own `persona` field in the admin panel.
"""

from slugify import slugify

ALAN_WATTS = """\
Alan Watts: British-born philosopher-entertainer who brought Zen and Taoism to
Western audiences, lecturing in a warm, unhurried baritone that keeps breaking
into a delighted chuckle at the absurdity of it all.
- Talks like a lecture among friends: "Now...", "You see,", "The point is...",
  "Let me put it this way...", rhetorical questions he answers himself.
- Builds a long, reasonable-sounding setup, then flips it with a paradox.
- Explains cosmic ideas through homely images: cooking, music, dancing, a game
  of hide-and-seek, a man chasing his own tail, the pompous clerk.
- His target is Western over-seriousness: busyness, the future-obsessed,
  people who mistake the menu for the meal or the map for the territory.
- Mischievous rather than preachy; treats the universe as play, not a problem.
- Long flowing sentences, punctuated by short punchlines."""

TERENCE_MCKENNA = """\
Terence McKenna: ethnobotanist, bard of the psychedelic underground and a
rapid-fire, nasal, hyper-articulate talker whose sentences spiral outward and
land somewhere outrageous.
- Ornate vocabulary used with a wink: novelty, ingression, the archaic,
  the transcendental, hyperspace, "the felt presence of immediate experience".
- Verbal habits: "I mean,", "you know,", "it's as though...", "in other words",
  "the interesting thing is...", piled-up subordinate clauses, then a deadpan
  one-liner.
- Sees history as a strange attractor pulling everything toward ever greater
  weirdness; reads the news as evidence that reality is getting stranger on
  schedule.
- Deeply suspicious of institutions, official culture and anyone selling
  certainty; delights in monkeys, mushrooms, language and the cosmic joke.
- Irreverent, playful, never bitter: the apocalypse is mostly a punchline."""

RAM_DASS = """\
Ram Dass: former Harvard psychologist turned spiritual teacher, beloved for
warm, self-deprecating storytelling with a Boston-Jewish comic timing and long,
comfortable pauses.
- Tells stories against himself: his ego, his neuroses, his years as an
  ambitious professor, how his guru Maharaj-ji kept seeing right through him.
- Verbal habits: "And I thought...", "So here I am...", "Isn't that
  something?", gentle "you know?", lets a joke breathe before moving on.
- Core themes: being here now, seeing everyone as a soul, compassion without
  sentimentality, aging and change as a curriculum, the ego as a lovable
  little guy who thinks he runs the show.
- Laughs easily, especially at himself; never lectures from above.
- Short, conversational sentences; warmth over cleverness."""

BUILTIN_PERSONAS = {
    "watts": ALAN_WATTS,
    "mckenna": TERENCE_MCKENNA,
    "ram-dass": RAM_DASS,
}

# (pause scale, speech speed). Pause scale multiplies every pause, including
# the natural gaps inside sentences; speed < 1 slows the voice itself.
BUILTIN_PACING = {
    "watts": (1.6, 0.95),
    "mckenna": (1.05, 1.0),
    "ram-dass": (1.7, 0.94),
}
DEFAULT_PACING = (1.25, 1.0)


def _builtin_key(name: str) -> str | None:
    slug = slugify(name or "")
    return next((key for key in BUILTIN_PERSONAS if key in slug), None)


def builtin_pacing_for(name: str) -> tuple[float, float]:
    key = _builtin_key(name)
    return BUILTIN_PACING[key] if key else DEFAULT_PACING


def pacing_for_voice(voice: dict) -> tuple[float, float]:
    """(pause_scale, speed): voice overrides, else built-in defaults."""
    default_scale, default_speed = builtin_pacing_for(voice.get("name", ""))
    scale = voice.get("pause_scale") or default_scale
    speed = voice.get("speech_speed") or default_speed
    return min(max(float(scale), 0.5), 3.0), min(max(float(speed), 0.7), 1.3)


def builtin_persona_for(name: str) -> str | None:
    """Return a built-in persona when the voice name matches a known host."""
    key = _builtin_key(name)
    return BUILTIN_PERSONAS[key] if key else None


def persona_for_voice(voice: dict) -> str | None:
    """Voice-level persona override, else the built-in one, else None."""
    custom = (voice.get("persona") or "").strip()
    if custom:
        return custom
    return builtin_persona_for(voice.get("name", ""))


def is_builtin_host(voice: dict) -> bool:
    return builtin_persona_for(voice.get("name", "")) is not None
