"""Folding a repo's updates into one digest per period.

Nothing is stored: the clock says which windows have closed and each item's date says which
window it belongs to, so every scan works out the same digests, restart or not.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from .feed import EPOCH, Item
from .source import Repo

_WALL_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class Schedule:
    """Numbered cutoffs every `period` seconds, lined up on `offset` seconds past midnight in `tz`.

    They're counted on the wall clock, so a daily cutoff keeps its time of day across DST changes.
    """

    period: float
    offset: float = 0
    tz: tzinfo = UTC

    def at(self, k: int) -> float:
        """When cutoff k happens."""
        wall = _WALL_EPOCH + timedelta(seconds=k * self.period + self.offset)
        return wall.replace(tzinfo=self.tz).timestamp()

    def index(self, t: float) -> int:
        """The number of the last cutoff at or before t."""
        wall = datetime.fromtimestamp(t, self.tz).replace(tzinfo=None)
        k = math.floor(((wall - _WALL_EPOCH).total_seconds() - self.offset) / self.period)
        # The wall clock jumps at DST changes, so check the guess against the real time.
        while self.at(k + 1) <= t:
            k += 1
        while self.at(k) > t:
            k -= 1
        return k


def _first_line(s: str) -> str:
    return s.split("\n", 1)[0].strip()


def _section(i: Item, tz: tzinfo) -> str:
    title = html.escape(_first_line(i.title))
    head = f'<a href="{html.escape(i.link)}">{title}</a>' if i.link else title
    meta = [i.updated.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")] if i.updated != EPOCH else []
    if i.author:
        meta.append(html.escape(i.author))
    line = "<p><small>" + " &middot; ".join(meta) + "</small></p>" if meta else ""
    return f"<h3>{head}</h3>{line}{i.content}"


def _digest(repo: Repo, kind: str, items: list[Item], cutoff: float, tz: tzinfo) -> Item:
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
        content="".join(_section(i, tz) for i in items),
    )


def summarize(repo: Repo, kind: str, items: list[Item], now: float, schedule: Schedule, keep: int) -> list[Item]:
    """The digests of the last `keep` closed windows, newest first. Quiet windows have none."""
    last = schedule.index(now)
    windows: dict[int, list[Item]] = {}
    for i in items:
        closes = schedule.index(i.updated.timestamp()) + 1
        if last - keep < closes <= last:
            windows.setdefault(closes, []).append(i)
    return [_digest(repo, kind, windows[k], schedule.at(k), schedule.tz) for k in sorted(windows, reverse=True)]


def held(items: list[Item], now: float, schedule: Schedule) -> int:
    """How many items are waiting for the next cutoff."""
    last = schedule.index(now)
    return sum(schedule.index(i.updated.timestamp()) >= last for i in items)
