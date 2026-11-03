"""Folding a repo's updates into one digest per period.

Nothing is stored: the clock says which windows have closed and each item's date says which
window it belongs to, so every scan works out the same digests, restart or not.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from .feed import EPOCH, Item
from .source import Repo


@dataclass(frozen=True)
class Schedule:
    """Cutoffs every `period` seconds, lined up on `offset` seconds past midnight UTC."""

    period: float
    offset: float = 0

    def last_cutoff(self, t: float) -> float:
        return math.floor((t - self.offset) / self.period) * self.period + self.offset


def _first_line(s: str) -> str:
    return s.split("\n", 1)[0].strip()


def _section(i: Item) -> str:
    title = html.escape(_first_line(i.title))
    head = f'<a href="{html.escape(i.link)}">{title}</a>' if i.link else title
    meta = [i.updated.strftime("%Y-%m-%d %H:%M UTC")] if i.updated != EPOCH else []
    if i.author:
        meta.append(html.escape(i.author))
    line = "<p><small>" + " &middot; ".join(meta) + "</small></p>" if meta else ""
    return f"<h3>{head}</h3>{line}{i.content}"


def _digest(repo: Repo, kind: str, items: list[Item], cutoff: float) -> Item:
    items = sorted(items, key=lambda i: i.updated, reverse=True)
    n = len(items)
    newest = items[0]
    what = f"{n} new {kind}" + ("" if n == 1 else "s")
    sep = ":" if n == 1 else ", latest"
    return Item(
        id=f"urn:dockfeed:summary:{repo}:{int(cutoff)}",
        title=f"{what}{sep} {_first_line(newest.title)}",
        link=newest.link if n == 1 and newest.link else repo.url,
        updated=datetime.fromtimestamp(cutoff, UTC),
        content="".join(_section(i) for i in items),
    )


def summarize(repo: Repo, kind: str, items: list[Item], now: float, schedule: Schedule, keep: int) -> list[Item]:
    """The digests of the last `keep` closed windows, newest first. Quiet windows have none."""
    last = schedule.last_cutoff(now)
    oldest = last - keep * schedule.period
    windows: dict[float, list[Item]] = {}
    for i in items:
        t = i.updated.timestamp()
        cutoff = schedule.last_cutoff(t) + schedule.period
        if oldest < cutoff <= last:
            windows.setdefault(cutoff, []).append(i)
    return [_digest(repo, kind, windows[c], c) for c in sorted(windows, reverse=True)]


def held(items: list[Item], now: float, schedule: Schedule) -> int:
    """How many items are waiting for the next cutoff."""
    last = schedule.last_cutoff(now)
    return sum(i.updated.timestamp() >= last for i in items)
