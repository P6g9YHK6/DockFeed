"""Holding back a repo's updates and publishing them as one digest entry per period."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .feed import EPOCH, Item
from .source import Repo

log = logging.getLogger("dockfeed")

VERSION = 1
FILENAME = "summaries.json"
MAX_DIGEST_ITEMS = 100
MAX_SEEN = 500
# Long enough that stopping a container for a while doesn't reset its summaries.
FORGET_AFTER = 30 * 86400


@dataclass
class Digest:
    item: Item
    kind: str


@dataclass
class _State:
    kind: str
    seen: list[str] = field(default_factory=list)
    pending: list[Item] = field(default_factory=list)
    pending_kind: str = ""
    dropped: int = 0
    opened: float = 0
    last_seen: float = 0
    digests: list[Digest] = field(default_factory=list)


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


def _digest(repo: Repo, s: _State, now: float) -> Digest:
    n = len(s.pending) + s.dropped
    newest = s.pending[0]
    what = f"{n} new {s.pending_kind}" + ("" if n == 1 else "s")
    sep = ":" if n == 1 else ", latest"
    content = "".join(_section(i) for i in s.pending)
    if s.dropped:
        content += f"<p>and {s.dropped} more</p>"
    item = Item(
        id=f"urn:dockfeed:summary:{repo}:{int(s.opened)}",
        title=f"{what}{sep} {_first_line(newest.title)}",
        link=newest.link if n == 1 and newest.link else repo.url,
        updated=datetime.fromtimestamp(now, UTC),
        content=content,
    )
    return Digest(item, s.pending_kind)


def _load_item(d: dict[str, Any]) -> Item:
    return Item(**{**d, "updated": datetime.fromisoformat(d["updated"])})


def _load_state(d: dict[str, Any]) -> _State:
    return _State(
        kind=d["kind"],
        seen=list(d["seen"]),
        pending=[_load_item(i) for i in d["pending"]],
        pending_kind=d["pending_kind"],
        dropped=d["dropped"],
        opened=d["opened"],
        last_seen=d["last_seen"],
        digests=[Digest(_load_item(x["item"]), x["kind"]) for x in d["digests"]],
    )


class SummaryStore:
    """Per-repo buffers and finished digests, in memory and optionally in a JSON file."""

    def __init__(self, data_dir: str, period: float, keep: int) -> None:
        self.path = os.path.join(data_dir, FILENAME) if data_dir else ""
        self.period = period
        self.keep = keep
        self.error = ""
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._dirty = False
        self._blocked = False

    def observe(self, repo: Repo, kind: str, items: list[Item], now: float) -> None:
        """Buffer the items not seen before. The first look at a repo only records what's already out."""
        ids = [i.id for i in items]
        with self._lock:
            s = self._states.get(str(repo))
            if s is None:
                s = self._states[str(repo)] = _State(kind)
                self._dirty = True
            elif s.kind != kind:
                # auto mode flipped between releases and commits: a different list, not new updates.
                s.kind = kind
                self._dirty = True
            else:
                self._buffer(s, kind, [i for i in items if i.id not in s.seen], now)
            seen = list(dict.fromkeys(ids + s.seen))[:MAX_SEEN]
            if seen != s.seen:
                s.seen = seen
                self._dirty = True
            s.last_seen = now

    def _buffer(self, s: _State, kind: str, new: list[Item], now: float) -> None:
        if not new:
            return
        if not s.pending:
            s.opened = now
            s.pending_kind = kind
        pending = sorted(new + s.pending, key=lambda i: i.updated, reverse=True)
        s.pending = pending[:MAX_DIGEST_ITEMS]
        s.dropped += len(pending) - len(s.pending)
        self._dirty = True

    def flush(self, repo: Repo, now: float) -> None:
        """Turn the buffer into a digest once its window has run out."""
        with self._lock:
            s = self._states.get(str(repo))
            if s is None:
                return
            s.last_seen = now
            if s.pending and now - s.opened >= self.period:
                s.digests = [_digest(repo, s, now), *s.digests][: self.keep]
                s.pending, s.dropped, s.opened, s.pending_kind = [], 0, 0, ""
                self._dirty = True

    def digests(self, repo: Repo) -> list[Digest]:
        with self._lock:
            s = self._states.get(str(repo))
            return s.digests[: self.keep] if s else []

    def pending(self, repo: Repo) -> tuple[int, float | None]:
        """How many updates are waiting, and when their digest is due."""
        with self._lock:
            s = self._states.get(str(repo))
            if not s or not s.pending:
                return 0, None
            return len(s.pending) + s.dropped, s.opened + self.period

    def retain(self, direct: Iterable[Repo], now: float) -> None:
        """Forget repos switched to direct, and repos no container has used for a long time."""
        gone = {str(r) for r in direct}
        with self._lock:
            for key, s in list(self._states.items()):
                if key in gone or now - s.last_seen > FORGET_AFTER:
                    del self._states[key]
                    self._dirty = True

    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if data["version"] != VERSION:
                raise ValueError(f"unknown version {data['version']!r}")
            states = {k: _load_state(v) for k, v in data["repos"].items()}
        except FileNotFoundError:
            return
        except OSError as e:
            # The file may be fine and just out of reach, so it must not be overwritten.
            self._blocked = True
            self._fail(f"read {self.path}: {e}")
            return
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("ignoring corrupt %s: %s", self.path, e)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError as moved:
                self._blocked = True
                self._fail(f"move aside {self.path}: {moved}")
            return
        with self._lock:
            self._states = states

    def save(self) -> None:
        if not self.path or self._blocked:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {"version": VERSION, "repos": {k: asdict(s) for k, s in self._states.items()}}
            body = json.dumps(data, default=datetime.isoformat)
            self._dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, self.path)
        except OSError as e:
            with self._lock:
                self._dirty = True
            self._fail(f"write {self.path}: {e}")
            return
        self.error = ""

    def _fail(self, msg: str) -> None:
        if msg != self.error:
            log.warning("summaries stay in memory only: %s", msg)
        self.error = msg
