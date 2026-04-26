from __future__ import annotations

import argparse
import hashlib
import html
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import feedparser
import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "feeds.yml"
DEFAULT_OUTPUT = ROOT / "public" / "feed.xml"


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
    # Keep summaries readable even when source feeds include escaped HTML snippets.
    return html.unescape(value or "").strip()


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a personalized RSS feed from public feeds.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    config = load_config(args.config)
    stories = collect_stories(config)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree = build_rss(config, stories)
    tree.write(args.output, encoding="utf-8", xml_declaration=True)

    print(f"Wrote {min(len(stories), int(config['feed'].get('max_items', 50)))} items to {args.output}")


if __name__ == "__main__":
    main()
