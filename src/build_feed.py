from __future__ import annotations

import argparse
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import feedparser
import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "feeds.yml"
DEFAULT_OUTPUT = ROOT / "public" / "feed.xml"
DEFAULT_PUBLIC_DIR = ROOT / "public"


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass(frozen=True)
class Story:
    title: str
    link: str
    summary: str
    source: str
    published: datetime
    guid: str


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def normalized_terms(values: list[str] | None) -> list[str]:
    return [value.casefold().strip() for value in values or [] if value.strip()]


def entry_text(entry: Any, source_name: str) -> str:
    tags = " ".join(tag.get("term", "") for tag in entry.get("tags", []))
    parts = [
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("description", ""),
        entry.get("link", ""),
        source_name,
        tags,
    ]
    return " ".join(parts).casefold()


def matches_filters(entry: Any, source_name: str, include: list[str], exclude: list[str]) -> bool:
    text = entry_text(entry, source_name)
    if include and not any(term in text for term in include):
        return False
    if exclude and any(term in text for term in exclude):
        return False
    return True


def parse_date(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def story_guid(entry: Any, link: str, source_name: str) -> str:
    raw_guid = entry.get("id") or entry.get("guid")
    if raw_guid:
        return str(raw_guid)
    digest = hashlib.sha256(f"{source_name}|{link}|{entry.get('title', '')}".encode()).hexdigest()
    return f"jak-news-{digest[:24]}"


def clean_summary(value: str) -> str:
    # Source feeds often include escaped paragraphs, lists, and links. Keep the
    # generated feed and browser page readable by turning those snippets into text.
    extractor = TextExtractor()
    extractor.feed(html.unescape(value or ""))
    text = extractor.text() or html.unescape(value or "")
    return re.sub(r"\s+", " ", text).strip()


def collect_stories(config: dict[str, Any]) -> list[Story]:
    include = normalized_terms(config.get("filters", {}).get("include_keywords"))
    exclude = normalized_terms(config.get("filters", {}).get("exclude_keywords"))
    stories: list[Story] = []

    for source in config.get("sources", []):
        source_name = source["name"]
        parsed = feedparser.parse(source["url"])

        if parsed.bozo:
            print(f"Warning: {source_name} may not have parsed cleanly: {parsed.bozo_exception}")

        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            title = entry.get("title", "").strip()
            if not link or not title:
                continue
            if not matches_filters(entry, source_name, include, exclude):
                continue

            stories.append(
                Story(
                    title=title,
                    link=link,
                    summary=clean_summary(entry.get("summary", entry.get("description", ""))),
                    source=source_name,
                    published=parse_date(entry),
                    guid=story_guid(entry, link, source_name),
                )
            )

    return dedupe_stories(stories)


def canonical_key(story: Story) -> str:
    parsed = urlparse(story.link)
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc.casefold()}{path}".casefold()


def dedupe_stories(stories: list[Story]) -> list[Story]:
    seen: set[str] = set()
    unique: list[Story] = []

    for story in sorted(stories, key=lambda item: item.published, reverse=True):
        key = canonical_key(story)
        if key in seen:
            continue
        seen.add(key)
        unique.append(story)

    return unique


def add_text(parent: ET.Element, tag: str, text: str) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = text
    return child


def build_rss(config: dict[str, Any], stories: list[Story]) -> ET.ElementTree:
    feed_config = config["feed"]
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    add_text(channel, "title", feed_config["title"])
    add_text(channel, "link", feed_config["link"])
    add_text(channel, "description", feed_config["description"])
    add_text(channel, "language", feed_config.get("language", "en"))
    add_text(channel, "lastBuildDate", format_datetime(datetime.now(timezone.utc)))
    add_text(channel, "generator", "Jak RSS Project")

    for story in stories[: int(feed_config.get("max_items", 50))]:
        item = ET.SubElement(channel, "item")
        add_text(item, "title", story.title)
        add_text(item, "link", story.link)
        add_text(item, "guid", story.guid).set("isPermaLink", "false")
        add_text(item, "pubDate", format_datetime(story.published))
        add_text(item, "source", story.source)
        add_text(item, "description", f"{story.summary}\n\nSource: {story.source}".strip())

    ET.indent(rss, space="  ")
    return ET.ElementTree(rss)


def write_rss(tree: ET.ElementTree, output: Path) -> None:
    xml = ET.tostring(tree.getroot(), encoding="unicode")
    output.write_text(
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<?xml-stylesheet type=\"text/xsl\" href=\"rss.xsl\"?>\n"
        f"{xml}\n",
        encoding="utf-8",
    )


def display_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def short_summary(value: str, limit: int = 360) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rsplit(" ", 1)[0] + "..."


def render_index(config: dict[str, Any], stories: list[Story], public_dir: Path) -> None:
    feed_config = config["feed"]
    visible_stories = stories[: int(feed_config.get("max_items", 50))]
    sources = sorted({story.source for story in visible_stories})
    latest = display_date(visible_stories[0].published) if visible_stories else "No stories yet"

    source_buttons = "\n".join(
        f'<button class="chip" type="button" data-source="{html.escape(source)}">{html.escape(source)}</button>'
        for source in sources
    )
    items = "\n".join(
        f"""
        <article class="story" data-source="{html.escape(story.source)}">
          <div class="story-meta">
            <span>{html.escape(story.source)}</span>
            <time datetime="{story.published.isoformat()}">{display_date(story.published)}</time>
          </div>
          <h2><a href="{html.escape(story.link)}" target="_blank" rel="noopener noreferrer">{html.escape(story.title)}</a></h2>
          <p>{html.escape(short_summary(story.summary))}</p>
          <a class="read-link" href="{html.escape(story.link)}" target="_blank" rel="noopener noreferrer">Read story</a>
        </article>
        """.strip()
        for story in visible_stories
    )

    index = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(feed_config["title"])}</title>
    <link rel="alternate" type="application/rss+xml" title="{html.escape(feed_config["title"])}" href="feed.xml">
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <header class="site-header">
      <div>
        <p class="eyebrow">Personal news feed</p>
        <h1>{html.escape(feed_config["title"])}</h1>
        <p class="lede">{html.escape(feed_config["description"])}</p>
      </div>
      <a class="rss-button" href="feed.xml">RSS</a>
    </header>

    <main>
      <section class="toolbar" aria-label="Feed controls">
        <label class="search">
          <span>Search</span>
          <input id="search" type="search" placeholder="Search stories, sources, topics">
        </label>
        <div class="stats">
          <strong>{len(visible_stories)}</strong>
          <span>stories</span>
          <strong>{html.escape(latest)}</strong>
          <span>latest</span>
        </div>
      </section>

      <nav class="chips" aria-label="Filter by source">
        <button class="chip active" type="button" data-source="all">All sources</button>
        {source_buttons}
      </nav>

      <section id="stories" class="story-list" aria-live="polite">
        {items}
      </section>

      <p id="empty" class="empty" hidden>No matching stories.</p>
    </main>

    <script>
      const search = document.querySelector("#search");
      const stories = [...document.querySelectorAll(".story")];
      const buttons = [...document.querySelectorAll(".chip")];
      const empty = document.querySelector("#empty");
      let activeSource = "all";

      function applyFilters() {{
        const query = search.value.trim().toLowerCase();
        let shown = 0;
        stories.forEach((story) => {{
          const sourceMatches = activeSource === "all" || story.dataset.source === activeSource;
          const textMatches = !query || story.textContent.toLowerCase().includes(query);
          const visible = sourceMatches && textMatches;
          story.hidden = !visible;
          if (visible) shown += 1;
        }});
        empty.hidden = shown !== 0;
      }}

      buttons.forEach((button) => {{
        button.addEventListener("click", () => {{
          activeSource = button.dataset.source;
          buttons.forEach((item) => item.classList.toggle("active", item === button));
          applyFilters();
        }});
      }});

      search.addEventListener("input", applyFilters);
    </script>
  </body>
</html>
"""
    public_dir.joinpath("index.html").write_text(index, encoding="utf-8")


def render_styles(public_dir: Path) -> None:
    public_dir.joinpath("styles.css").write_text(
        """* {
  box-sizing: border-box;
}

:root {
  color-scheme: light;
  --ink: #18212f;
  --muted: #5d6878;
  --line: #d9e0e8;
  --paper: #f5f7fa;
  --surface: #ffffff;
  --accent: #0d6e6e;
  --accent-dark: #084f4f;
}

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
}

a {
  color: inherit;
}

.site-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 36px max(20px, calc((100vw - 1100px) / 2)) 24px;
  background: #ffffff;
  border-bottom: 1px solid var(--line);
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1 {
  margin: 0;
  font-size: clamp(2rem, 5vw, 4.25rem);
  line-height: 0.95;
}

.lede {
  max-width: 680px;
  margin: 14px 0 0;
  color: var(--muted);
  font-size: 1.05rem;
}

.rss-button,
.read-link {
  display: inline-flex;
  align-items: center;
  min-height: 40px;
  border-radius: 6px;
  text-decoration: none;
  font-weight: 800;
}

.rss-button {
  padding: 0 16px;
  background: var(--ink);
  color: #ffffff;
}

main {
  width: min(1100px, calc(100% - 40px));
  margin: 24px auto 64px;
}

.toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) auto;
  gap: 16px;
  align-items: end;
  margin-bottom: 14px;
}

.search span {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 0.85rem;
  font-weight: 700;
}

.search input {
  width: 100%;
  min-height: 46px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0 14px;
  background: #ffffff;
  color: var(--ink);
  font: inherit;
}

.stats {
  display: grid;
  grid-template-columns: auto auto;
  gap: 2px 8px;
  color: var(--muted);
  font-size: 0.88rem;
}

.stats strong {
  color: var(--ink);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 20px;
}

.chip {
  min-height: 36px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0 13px;
  background: #ffffff;
  color: var(--ink);
  cursor: pointer;
  font: inherit;
  font-weight: 700;
}

.chip.active {
  border-color: var(--accent);
  background: var(--accent);
  color: #ffffff;
}

.story-list {
  display: grid;
  gap: 10px;
}

.story {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  background: var(--surface);
}

.story-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-bottom: 8px;
  color: var(--muted);
  font-size: 0.86rem;
  font-weight: 700;
}

.story h2 {
  margin: 0;
  font-size: 1.18rem;
  line-height: 1.25;
}

.story h2 a {
  text-decoration: none;
}

.story h2 a:hover {
  color: var(--accent-dark);
  text-decoration: underline;
}

.story p {
  margin: 10px 0 12px;
  color: var(--muted);
}

.read-link {
  color: var(--accent-dark);
}

.empty {
  padding: 32px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  background: #ffffff;
  color: var(--muted);
  text-align: center;
}

@media (max-width: 720px) {
  .site-header,
  .toolbar {
    display: block;
  }

  .rss-button {
    margin-top: 18px;
  }

  .stats {
    margin-top: 12px;
  }
}
""",
        encoding="utf-8",
    )


def render_xsl(public_dir: Path) -> None:
    public_dir.joinpath("rss.xsl").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html" encoding="UTF-8" doctype-system="about:legacy-compat"/>
  <xsl:template match="/rss/channel">
    <html lang="en">
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title><xsl:value-of select="title"/></title>
        <link rel="stylesheet" href="styles.css"/>
      </head>
      <body>
        <header class="site-header">
          <div>
            <p class="eyebrow">RSS feed</p>
            <h1><xsl:value-of select="title"/></h1>
            <p class="lede"><xsl:value-of select="description"/></p>
          </div>
          <a class="rss-button" href="./">Readable page</a>
        </header>
        <main>
          <section class="story-list">
            <xsl:for-each select="item">
              <article class="story">
                <div class="story-meta">
                  <span><xsl:value-of select="source"/></span>
                  <time><xsl:value-of select="pubDate"/></time>
                </div>
                <h2><a target="_blank" rel="noopener noreferrer"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute><xsl:value-of select="title"/></a></h2>
                <p><xsl:value-of select="description"/></p>
                <a class="read-link" target="_blank" rel="noopener noreferrer"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>Read story</a>
              </article>
            </xsl:for-each>
          </section>
        </main>
      </body>
    </html>
  </xsl:template>
</xsl:stylesheet>
""",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a personalized RSS feed from public feeds.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    config = load_config(args.config)
    stories = collect_stories(config)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree = build_rss(config, stories)
    write_rss(tree, args.output)
    render_index(config, stories, args.output.parent)
    render_styles(args.output.parent)
    render_xsl(args.output.parent)

    visible_count = min(len(stories), int(config["feed"].get("max_items", 50)))
    print(f"Wrote {visible_count} items to {args.output}")
    print(f"Wrote readable page to {args.output.parent / 'index.html'}")


if __name__ == "__main__":
    main()
