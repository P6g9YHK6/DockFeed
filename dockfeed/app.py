"""Configuration, the scan loop and the HTTP server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .discover import Discoverer
from .docker import DockerClient
from .feed import FeedEntry, Item, commit_message, render_atom
from .forge import Fetcher, NotFoundError
from .source import LABEL_PREFIX, Container, Repo, resolve_repo

log = logging.getLogger("dockfeed")

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> float:
    """Seconds from a duration like 90s, 1h30m or 365d. A bare number is seconds."""
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if not s or _DURATION.sub("", s):
        raise ValueError(f"invalid duration {s!r}")
    return sum(float(n) * _UNITS[u] for n, u in _DURATION.findall(s))


def _bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"invalid boolean {s!r}")


@dataclass
class Config:
    docker_host: str = "unix:///var/run/docker.sock"
    listen: str = ":8080"
    interval: float = 3600
    stale_after: float = 365 * 86400
    items_per_repo: int = 10
    max_items: int = 500
    concurrency: int = 8
    feed_title: str = "DockFeed"
    include_stopped: bool = False
    discover: bool = True
    github_token: str = ""
    host_name: str = ""
    exclude: re.Pattern[str] | None = None
    public_url: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> Config:
        def get(key: str, default: str) -> str:
            return env.get(key) or default

        c = cls()
        try:
            c.docker_host = get("DOCKER_HOST", c.docker_host)
            c.listen = get("LISTEN_ADDR", c.listen)
            c.feed_title = get("FEED_TITLE", c.feed_title)
            c.public_url = get("PUBLIC_URL", "").rstrip("/")
            c.github_token = get("GITHUB_TOKEN", "")
            c.host_name = get("HOST_NAME", "")
            c.interval = parse_duration(get("REFRESH_INTERVAL", "1h"))
            c.stale_after = parse_duration(get("STALE_RELEASE_AFTER", "365d"))
            positive = [
                ("items_per_repo", "ITEMS_PER_REPO"),
                ("max_items", "MAX_ITEMS"),
                ("concurrency", "CONCURRENCY"),
            ]
            for attr, key in positive:
                n = int(get(key, str(getattr(c, attr))))
                if n < 1:
                    raise ValueError(f"{key} must be a positive integer")
                setattr(c, attr, n)
            c.include_stopped = _bool(get("INCLUDE_STOPPED", "false"))
            c.discover = _bool(get("DISCOVER", "true"))
            if v := get("EXCLUDE", ""):
                c.exclude = re.compile(v)
        except re.error as e:
            raise ValueError(f"EXCLUDE: {e}") from e
        if c.interval <= 0:
            raise ValueError("REFRESH_INTERVAL must be positive")
        return c

    @property
    def bind(self) -> tuple[str, int]:
        host, _, port = self.listen.rpartition(":")
        return host.strip("[]") or "0.0.0.0", int(port)


@dataclass
class Target:
    """One repo to follow plus every container that runs code from it."""

    repo: Repo
    via: str
    mode: str = "auto"
    branch: str = ""
    containers: list[str] = field(default_factory=list)
    running: str = ""
    include: re.Pattern[str] | None = None
    exclude: re.Pattern[str] | None = None
    error: str = ""

    def keep(self, items: list[Item], global_exclude: re.Pattern[str] | None) -> list[Item]:
        """Apply the include/exclude title filters; a filtered-out release doesn't count as one."""
        exclude = self.exclude or global_exclude
        return [
            i
            for i in items
            if (not self.include or self.include.search(i.title)) and not (exclude and exclude.search(i.title))
        ]


class App:
    def __init__(self, cfg: Config, docker: DockerClient, fetcher: Fetcher) -> None:
        self.cfg = cfg
        self.docker = docker
        self.fetcher = fetcher
        self.discoverer = Discoverer(fetcher)
        self._lock = threading.Lock()
        self.entries: list[FeedEntry] = []
        self.status: dict[str, Any] = {"version": __version__, "last_scan": None}
        self.ready = False
        self.host = cfg.host_name
        self._host_warned = False

    def targets(self, containers: list[Container]) -> tuple[dict[str, Target], dict[str, str], list[str]]:
        targets: dict[str, Target] = {}
        unresolved: dict[str, str] = {}
        ignored: list[str] = []
        for c in containers:
            enable = c.labels.get(LABEL_PREFIX + "enable")
            if enable is not None:
                try:
                    if not _bool(enable):
                        ignored.append(c.name)
                        continue
                except ValueError:
                    pass
            found = resolve_repo(c)
            if not found and self.cfg.discover:
                found = self.discoverer.discover(c.image)
            if not found:
                unresolved[c.name] = c.image
                continue
            repo, via = found
            t = targets.setdefault(str(repo), Target(repo, via))
            t.containers.append(c.name)
            t.mode = c.labels.get(LABEL_PREFIX + "mode") or t.mode
            t.branch = c.labels.get(LABEL_PREFIX + "branch") or t.branch
            for attr in ("include", "exclude"):
                if v := c.labels.get(LABEL_PREFIX + attr):
                    try:
                        setattr(t, attr, re.compile(v))
                    except re.error as e:
                        t.error = f"{LABEL_PREFIX}{attr} on {c.name}: {e}"
            if not t.running:
                t.running = c.labels.get("org.opencontainers.image.version", "")
        for t in targets.values():
            t.containers.sort()
        return targets, unresolved, sorted(ignored)

    def collect(self, t: Target) -> tuple[list[Item], str]:
        """Fetch one target, falling back to commits when the repo doesn't publish releases."""
        if t.error:
            raise ValueError(t.error)
        ex = self.cfg.exclude
        if t.mode == "releases":
            return t.keep(self.fetcher.releases(t.repo), ex), "release"
        if t.mode == "commits":
            return t.keep(self.fetcher.commits(t.repo, t.branch), ex), "commit"
        if t.mode != "auto":
            raise ValueError(f"unknown {LABEL_PREFIX}mode {t.mode!r}")
        try:
            items = t.keep(self.fetcher.releases(t.repo), ex)
        except NotFoundError:
            items = []
        stale = self.cfg.stale_after
        if items and (not stale or time.time() - items[0].updated.timestamp() < stale):
            return items, "release"
        try:
            return t.keep(self.fetcher.commits(t.repo, t.branch), ex), "commit"
        except Exception:
            if items:
                return items, "release"
            raise

    def _fetch(self, t: Target) -> tuple[list[FeedEntry], dict[str, Any]]:
        rs: dict[str, Any] = {"repo": str(t.repo), "resolved_via": t.via, "containers": t.containers}
        items: list[Item] = []
        try:
            items, rs["kind"] = self.collect(t)
        except Exception as e:  # noqa: BLE001 - one broken repo must not sink the scan
            rs["error"] = str(e) or type(e).__name__
            log.warning("fetch failed repo=%s err=%s", t.repo, rs["error"])
        items = items[: self.cfg.items_per_repo]
        if rs.get("kind") == "commit":
            items = [commit_message(i) for i in items]
        label = t.containers[0] if len(t.containers) == 1 else t.repo.name
        rs["items"] = len(items)
        if items:
            rs["latest"] = items[0].updated.isoformat()
        entries = [FeedEntry(i, label, rs.get("kind", ""), t.repo, t.containers, t.running, self.host) for i in items]
        return entries, rs

    def refresh(self) -> None:
        start = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        try:
            containers = self.docker.containers(self.cfg.include_stopped)
        except OSError as e:
            log.error("docker scan failed: %s", e)
            with self._lock:
                self.status["scan_error"] = str(e)
            return
        self.host = self.cfg.host_name or self._engine_hostname()
        targets, unresolved, ignored = self.targets(containers)
        with ThreadPoolExecutor(self.cfg.concurrency) as pool:
            results = list(pool.map(self._fetch, targets.values()))
        # Not capped here: MAX_ITEMS applies after ?container= filtering, or a quiet container
        # would vanish from its own filtered feed behind 500 newer entries from busier ones.
        entries = sorted((e for es, _ in results for e in es), key=lambda e: e.item.updated, reverse=True)
        repos = sorted((rs for _, rs in results), key=lambda r: r["repo"])
        with self._lock:
            self.entries = entries
            self.status = {
                "version": __version__,
                "host": self.host,
                "last_scan": started_at,
                "repos": repos,
                "unresolved": unresolved,
                "ignored": ignored,
            }
            self.ready = True
        log.info(
            "refreshed containers=%d repos=%d unresolved=%d items=%d took=%.1fs",
            len(containers),
            len(targets),
            len(unresolved),
            len(entries),
            time.monotonic() - start,
        )

    def _engine_hostname(self) -> str:
        try:
            return self.docker.hostname()
        except (OSError, ValueError, AttributeError) as e:
            # Socket proxies often block /info; the feed just goes without a host then.
            if not self._host_warned:
                log.warning("could not read the host name from /info (%s); set HOST_NAME or allow INFO on the proxy", e)
                self._host_warned = True
            return ""

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("scan crashed")
            stop.wait(self.cfg.interval)

    def feed(self, query: dict[str, list[str]], self_url: str) -> bytes | None:
        with self._lock:
            entries, ready = self.entries, self.ready
        if not ready:
            return None
        # ?container=a,b narrows the feed; ?kind=release drops commit fallbacks.
        if q := query.get("container"):
            want = {n.strip() for n in q[0].split(",")}
            entries = [e for e in entries if want.intersection(e.containers)]
        if k := query.get("kind"):
            entries = [e for e in entries if e.kind == k[0]]
        return render_atom(self.cfg.feed_title, self_url, entries[: self.cfg.max_items])


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"dockfeed/{__version__}"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urlsplit(self.path)
            if u.path in ("/", "/feed.atom"):
                base = app.cfg.public_url
                if not base:
                    proto = self.headers.get("X-Forwarded-Proto", "http")
                    base = f"{proto}://{self.headers.get('Host', 'localhost')}"
                body = app.feed(parse_qs(u.query), base + self.path)
                if body is None:
                    self._send(503, b"first scan still running\n", "text/plain")
                else:
                    self._send(200, body, "application/atom+xml; charset=utf-8")
            elif u.path == "/status":
                with app._lock:
                    body = json.dumps(app.status, indent=2).encode()
                self._send(200, body, "application/json")
            elif u.path == "/healthz":
                self._send(200, b"ok\n", "text/plain")
            else:
                self._send(404, b"not found\n", "text/plain")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug(format, *args)

    return Handler


def healthcheck(cfg: Config) -> int:
    host, port = cfg.bind
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dockfeed", description="One Atom feed for the releases of every container.")
    p.add_argument("--healthcheck", action="store_true", help="probe the running server and exit 0 if healthy")
    p.add_argument("--once", action="store_true", help="scan once, print the feed to stdout and exit")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if args.healthcheck:
        return healthcheck(cfg)
    try:
        docker = DockerClient(cfg.docker_host)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    fetcher = Fetcher(f"dockfeed/{__version__} (+https://github.com/P6g9YHK6/DockFeed)", cfg.github_token)
    app = App(cfg, docker, fetcher)

    if args.once:
        app.refresh()
        sys.stdout.buffer.write(app.feed({}, "urn:stdout") or b"")
        return 0

    stop = threading.Event()
    server = ThreadingHTTPServer(cfg.bind, make_handler(app))
    server.daemon_threads = True

    def shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=app.loop, args=(stop,), daemon=True, name="scan").start()
    log.info("listening addr=%s docker=%s interval=%ss", cfg.listen, cfg.docker_host, int(cfg.interval))
    server.serve_forever()
    return 0
