"""Fetch world headlines from public RSS/Atom feeds (stdlib only)."""

import asyncio
import html
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

logger = logging.getLogger("radionow.news")

# (source label, url, section). "world" stories are the big ones; "light"
# feeds give hosts gentler material to play with.
DEFAULT_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
    ("The Guardian", "https://www.theguardian.com/world/rss", "world"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "world"),
    ("NPR", "https://feeds.npr.org/1004/rss.xml", "world"),
    ("DW", "https://rss.dw.com/rdf/rss-en-world", "world"),
    ("France 24", "https://www.france24.com/en/rss", "world"),
    ("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "world"),
    ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "light"),
    ("UPI Odd News", "https://rss.upi.com/news/odd_news.rss", "light"),
    ("Positive News", "https://www.positive.news/feed/", "light"),
]

PER_FEED_LIMIT = {"world": 12, "light": 6}
SUMMARY_CHARS = 240
USER_AGENT = "RadioNow/1.0 (+https://now.breathdance.net)"


@dataclass
class Headline:
    source: str
    section: str
    title: str
    summary: str
    link: str

    def as_dict(self) -> dict:
        return asdict(self)


def configured_feeds() -> list[tuple[str, str, str]]:
    """Feeds from NEWS_FEEDS ("Label|url|section" per line or ';'), else defaults."""
    raw = os.getenv("NEWS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    feeds = []
    for entry in re.split(r"[;\n]", raw):
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) >= 2 and parts[1].startswith("http"):
            feeds.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "world"))
    return feeds or DEFAULT_FEEDS


def _clean(text: str | None) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    # WordPress feeds append "The post X appeared first on Y."
    text = re.sub(r"\s*The post .*? appeared first on .*$", "", text, flags=re.S)
    return " ".join(text.split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local(child.tag) in names:
            if _local(child.tag) == "link" and child.get("href"):
                return child.get("href", "")
            return child.text or ""
    return ""


def parse_feed(xml_bytes: bytes, source: str, section: str, limit: int) -> list[Headline]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into headlines."""
    root = ET.fromstring(xml_bytes)
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    headlines = []
    for item in items[:limit]:
        title = _clean(_child_text(item, "title"))
        if not title:
            continue
        summary = _clean(_child_text(item, "description", "summary", "content"))
        if len(summary) > SUMMARY_CHARS:
            summary = summary[: SUMMARY_CHARS - 3].rsplit(" ", 1)[0] + "..."
        link = _child_text(item, "link").strip()
        headlines.append(Headline(source, section, title, summary, link))
    return headlines


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


async def fetch_feed(source: str, url: str, section: str) -> list[Headline]:
    try:
        data = await asyncio.to_thread(_fetch, url)
        limit = PER_FEED_LIMIT.get(section, 8)
        return parse_feed(data, source, section, limit)
    except Exception as e:
        logger.warning("Feed %s failed: %s", source, e)
        return []


async def fetch_headlines() -> list[Headline]:
    """Fetch all configured feeds concurrently; drop duplicate titles."""
    results = await asyncio.gather(
        *(fetch_feed(source, url, section) for source, url, section in configured_feeds())
    )
    seen, headlines = set(), []
    for feed_items in results:
        for item in feed_items:
            key = item.title.lower()
            if key not in seen:
                seen.add(key)
                headlines.append(item)
    logger.info("Fetched %d headlines from %d feeds", len(headlines), len(results))
    return headlines


def format_headlines(headlines: list[Headline]) -> str:
    """Render headlines as a compact wire digest for the prompt."""
    blocks = {"world": [], "light": []}
    for h in headlines:
        line = f"- [{h.source}] {h.title}"
        if h.summary:
            line += f" -- {h.summary}"
        blocks.setdefault(h.section, []).append(line)
    out = []
    if blocks.get("world"):
        out.append("WORLD WIRE:\n" + "\n".join(blocks["world"]))
    if blocks.get("light"):
        out.append("LIGHTER ITEMS (science, oddities, good news):\n" + "\n".join(blocks["light"]))
    return "\n\n".join(out)
