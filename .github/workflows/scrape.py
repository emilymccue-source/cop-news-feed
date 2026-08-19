#!/usr/bin/env python3
"""
Build an RSS 2.0 feed from the ConocoPhillips spiritnow story archive.

Design notes
------------
The archive page has no official feed, so we scrape it. To keep this from
breaking every time ConocoPhillips touches their CSS, the scraper deliberately
avoids class-name selectors:

  1. Story URLs come from the archive listing by matching the URL *path*
     (/spiritnow/story/...), which is far more stable than markup.
  2. All per-story metadata (title, image, date, summary) comes from each story
     page's Open Graph / JSON-LD tags. Those are standard, and ConocoPhillips
     maintains them for social sharing, so they change rarely.

The archive's pagination is client-side JavaScript, so only page 1 (the newest
~12 stories) is reachable without a headless browser. We work around that by
keeping a small state file: every run merges newly seen stories into the
previously seen ones, so the feed accumulates history over time instead of
being capped at 12 forever.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

ARCHIVE_URL = "https://www.conocophillips.com/spiritnow/archive/"
SITE_URL = "https://www.conocophillips.com/spiritnow/"
STORY_PATH = "/spiritnow/story/"

FEED_TITLE = "ConocoPhillips News"
FEED_DESCRIPTION = (
    "Feature stories and employee profiles from the ConocoPhillips "
    "spiritnow archive."
)

# Where things land. Overridable so the workflow can point elsewhere.
OUT_DIR = Path(os.environ.get("OUT_DIR", "."))
OUT_FILE = OUT_DIR / os.environ.get("OUT_FILE", "feed.xml")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen_stories.json"))

# The public URL the feed will be served from, used for the atom:self link.
FEED_SELF_URL = os.environ.get("FEED_SELF_URL", "")

MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))
TIMEOUT = 30

HEADERS = {
    # Identify honestly. If ConocoPhillips ever wants to rate-limit or block
    # this, they should be able to recognize it rather than guess.
    "User-Agent": (
        "cop-news-feed/1.0 (internal digital-signage feed builder; "
        "contact: emcmccue@gmail.com)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

session = requests.Session()
session.headers.update(HEADERS)


def log(msg):
    print(f"[cop-news-feed] {msg}", file=sys.stderr)


def fetch(url):
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def discover_story_urls(html):
    """Pull every /spiritnow/story/ link off the archive page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(ARCHIVE_URL, a["href"].strip())
        if STORY_PATH not in href:
            continue
        # Normalize: drop fragments and query strings, enforce trailing slash.
        href = href.split("#")[0].split("?")[0]
        if not href.endswith("/"):
            href += "/"
        if href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def meta(soup, *, prop=None, name=None):
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"].strip()
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def json_ld_blocks(soup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    yield entry
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                for entry in data["@graph"]:
                    if isinstance(entry, dict):
                        yield entry
            else:
                yield data


DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
)


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    # Python's %z doesn't like the colon in +00:00 on older versions.
    normalized = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", value)
    for fmt in DATE_FORMATS:
        for candidate in (normalized, value):
            try:
                dt = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None


def find_visible_date(soup):
    """Last resort: look for a 'Month DD, YYYY' string in the page body."""
    text = soup.get_text(" ", strip=True)
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+\d{4}\b",
        text,
    )
    return parse_date(match.group(0)) if match else None


def scrape_story(url):
    """Return a dict of feed-item fields for one story page."""
    html = fetch(url)
    soup = BeautifulSoup(html, "html.parser")

    title = (
        meta(soup, prop="og:title")
        or (soup.title.get_text(strip=True) if soup.title else None)
        or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    )
    # Story titles often carry a " | ConocoPhillips" suffix. Strip it.
    title = re.sub(r"\s*[|–-]\s*ConocoPhillips\s*$", "", title).strip()

    description = (
        meta(soup, prop="og:description")
        or meta(soup, name="description")
        or ""
    )

    image = meta(soup, prop="og:image")
    if image:
        image = urljoin(url, image)

    published = parse_date(
        meta(soup, prop="article:published_time")
        or meta(soup, prop="og:article:published_time")
        or meta(soup, name="publish_date")
        or meta(soup, name="date")
    )

    category = None
    if not published or not image:
        for block in json_ld_blocks(soup):
            if not published:
                published = parse_date(
                    block.get("datePublished") or block.get("dateCreated")
                )
            if not image:
                img = block.get("image")
                if isinstance(img, dict):
                    img = img.get("url")
                if isinstance(img, list) and img:
                    img = img[0]
                    if isinstance(img, dict):
                        img = img.get("url")
                if isinstance(img, str):
                    image = urljoin(url, img)
            if not category:
                section = block.get("articleSection")
                if isinstance(section, str):
                    category = section

    if not published:
        published = find_visible_date(soup)

    return {
        "url": url,
        "title": title,
        "description": description,
        "image": image,
        "category": category,
        "published": published.isoformat() if published else None,
        # When we first saw it. Guarantees a stable ordering key even for
        # stories that publish no date at all.
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            log("state file was corrupt; starting fresh")
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def sort_key(item):
    stamp = item.get("published") or item.get("first_seen")
    dt = parse_date(stamp) if stamp else None
    return dt or datetime.min.replace(tzinfo=timezone.utc)


def build_rss(items):
    now = format_datetime(datetime.now(timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" '
        'xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">',
        "  <channel>",
        f"    <title>{escape(FEED_TITLE)}</title>",
        f"    <link>{escape(SITE_URL)}</link>",
        f"    <description>{escape(FEED_DESCRIPTION)}</description>",
        "    <language>en-us</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        "    <ttl>60</ttl>",
        "    <generator>cop-news-feed</generator>",
    ]
    if FEED_SELF_URL:
        parts.append(
            f'    <atom:link href="{escape(FEED_SELF_URL)}" '
            'rel="self" type="application/rss+xml" />'
        )

    for item in items:
        pub = parse_date(item.get("published") or item.get("first_seen"))
        parts.append("    <item>")
        parts.append(f"      <title>{escape(item['title'])}</title>")
        parts.append(f"      <link>{escape(item['url'])}</link>")
        parts.append(
            f'      <guid isPermaLink="true">{escape(item["url"])}</guid>'
        )
        if item.get("description"):
            parts.append(
                f"      <description>{escape(item['description'])}</description>"
            )
        if item.get("category"):
            parts.append(f"      <category>{escape(item['category'])}</category>")
        if pub:
            parts.append(f"      <pubDate>{format_datetime(pub)}</pubDate>")
        if item.get("image"):
            img = escape(item["image"])
            # enclosure is what most signage RSS readers (Enplug included)
            # look for; media:content is the belt-and-braces version.
            parts.append(
                f'      <enclosure url="{img}" type="image/jpeg" length="0" />'
            )
            parts.append(f'      <media:content url="{img}" medium="image" />')
            parts.append(f'      <media:thumbnail url="{img}" />')
        parts.append("    </item>")

    parts.append("  </channel>")
    parts.append("</rss>")
    return "\n".join(parts) + "\n"


def main():
    log(f"fetching archive: {ARCHIVE_URL}")
    archive_html = fetch(ARCHIVE_URL)
    story_urls = discover_story_urls(archive_html)
    log(f"found {len(story_urls)} story links on the archive page")

    if not story_urls:
        # Fail loudly. A silently empty feed would blank the signage screens
        # without anyone noticing the scraper broke.
        log("ERROR: no story links found. The archive markup likely changed.")
        sys.exit(1)

    state = load_state()
    new_count = 0

    for url in story_urls:
        if url in state and state[url].get("title"):
            continue
        try:
            log(f"scraping {url}")
            state[url] = scrape_story(url)
            new_count += 1
            time.sleep(REQUEST_DELAY)
        except Exception as exc:  # noqa: BLE001 - one bad story shouldn't kill the run
            log(f"WARNING: failed to scrape {url}: {exc}")

    log(f"{new_count} new stories this run; {len(state)} known in total")

    items = sorted(state.values(), key=sort_key, reverse=True)[:MAX_ITEMS]

    if not items:
        log("ERROR: no items to publish.")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(build_rss(items), encoding="utf-8")
    save_state(state)
    log(f"wrote {OUT_FILE} with {len(items)} items")


if __name__ == "__main__":
    main()
