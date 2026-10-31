"""Reading Atom and RSS feeds, and writing the merged Atom feed."""

from __future__ import annotations

import calendar
import html
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import feedparser

from .source import Repo

EPOCH = datetime.fromtimestamp(0, UTC)
ATOM_NS = "http://www.w3.org/2005/Atom"


@dataclass
class Item:
    """A single feed entry, normalized from either Atom or RSS."""

    id: str
    title: str
    link: str = ""
    updated: datetime = EPOCH
    author: str = ""
    content: str = ""


@dataclass
class FeedEntry:
    """An Item attributed to the containers it concerns."""

    item: Item
    label: str
    kind: str  # "release" or "commit"
    repo: Repo
    containers: list[str] = field(default_factory=list)
    running: str = ""


def _date(entry: Any) -> datetime:
    # dict.get skips feedparser's deprecated updated/published aliasing.
    for key in ("updated_parsed", "published_parsed"):
        if t := dict.get(entry, key):
            return datetime.fromtimestamp(calendar.timegm(t), UTC)
    return EPOCH


def _content(entry: Any) -> str:
    for c in entry.get("content") or []:
        if c.get("value"):
            return _as_html(c)
    if entry.get("summary"):
        return _as_html(entry.get("summary_detail") or {"value": entry["summary"], "type": "text/html"})
    return ""


def _as_html(detail: Any) -> str:
    value = (detail.get("value") or "").strip()
    if detail.get("type") == "text/plain":
        return html.escape(value)
    return value


def parse_feed(data: bytes) -> list[Item]:
    """Parse Atom or RSS and return entries newest first."""
    parsed = feedparser.parse(data)
    if parsed.get("bozo") and not parsed.entries:
        raise ValueError(f"parse feed: {parsed.get('bozo_exception')}")
    items = []
    for e in parsed.entries:
        link = e.get("link", "")
        items.append(
            Item(
                id=e.get("id") or link,
                title=(e.get("title") or "").strip(),
                link=link,
                updated=_date(e),
                author=e.get("author", ""),
                content=_content(e),
            )
        )
    # Stable, so items with equal or missing dates keep the source order.
    items.sort(key=lambda i: i.updated, reverse=True)
    return items


def _headline(e: FeedEntry) -> str:
    if e.kind == "commit":
        # Commit titles can be whole messages; the first line is enough for a headline.
        first = e.item.title.split("\n", 1)[0].strip()
        return f"{e.label} (commit): {first}"
    return f"{e.label}: {e.item.title}"


def _footer(e: FeedEntry) -> str:
    parts = [
        f'<a href="{html.escape(e.repo.url)}">{html.escape(str(e.repo))}</a>',
        "used by " + html.escape(", ".join(e.containers)),
    ]
    if e.running:
        parts.append("running " + html.escape(e.running))
    return "<hr/><p>" + " &middot; ".join(parts) + "</p>"


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_atom(title: str, self_url: str, entries: list[FeedEntry]) -> bytes:
    ET.register_namespace("", ATOM_NS)

    def sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        el = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    root = ET.Element(f"{{{ATOM_NS}}}feed")
    sub(root, "id", "urn:dockfeed:" + quote(title))
    sub(root, "title", title)
    updated = max((e.item.updated for e in entries), default=None) or datetime.now(UTC)
    sub(root, "updated", _iso(updated))
    sub(root, "link", href=self_url, rel="self", type="application/atom+xml")
    for e in entries:
        entry = sub(root, "entry")
        sub(entry, "id", e.item.id)
        sub(entry, "title", _headline(e))
        sub(entry, "link", href=e.item.link, rel="alternate")
        sub(entry, "updated", _iso(e.item.updated))
        if e.item.author:
            sub(sub(entry, "author"), "name", e.item.author)
        for c in e.containers:
            sub(entry, "category", term=c)
        sub(entry, "category", term=e.kind)
        sub(entry, "content", e.item.content + _footer(e), type="html")
    ET.indent(root)
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
