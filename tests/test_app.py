import http.client
import re
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dockfeed.app import App, Config, Server, Target, make_handler, parse_duration
from dockfeed.feed import Item
from dockfeed.forge import NotFoundError
from dockfeed.source import Container, Repo


@pytest.mark.parametrize(
    ("s", "want"), [("1h", 3600), ("90s", 90), ("1h30m", 5400), ("365d", 365 * 86400), ("0", 0), ("8760h", 8760 * 3600)]
)
def test_parse_duration(s: str, want: float) -> None:
    assert parse_duration(s) == want


@pytest.mark.parametrize("s", ["", "1x", "h", "1h junk"])
def test_parse_duration_rejects(s: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(s)


def test_config_from_env() -> None:
    c = Config.from_env(
        {"REFRESH_INTERVAL": "30m", "EXCLUDE": "(?i)-rc", "LISTEN_ADDR": "127.0.0.1:9000", "DISCOVER": "false"}
    )
    assert c.interval == 1800
    assert c.exclude is not None and c.exclude.search("v1.0-RC1")
    assert c.bind == ("127.0.0.1", 9000)
    assert not c.discover
    assert Config.from_env({}).bind == ("0.0.0.0", 8080)
    with pytest.raises(ValueError):
        Config.from_env({"MAX_ITEMS": "0"})
    with pytest.raises(ValueError):
        Config.from_env({"EXCLUDE": "("})


def test_config_summary_settings() -> None:
    c = Config.from_env({})
    assert (c.feed_type, c.summary_period, c.data_dir) == ("direct", 86400, "")
    c = Config.from_env({"FEED_TYPE": "summary", "SUMMARY_PERIOD": "12h", "DATA_DIR": "/data"})
    assert (c.feed_type, c.summary_period, c.data_dir) == ("summary", 43200, "/data")
    for bad in ({"FEED_TYPE": "digest"}, {"SUMMARY_PERIOD": "0"}, {"SUMMARY_PERIOD": "soon"}):
        with pytest.raises(ValueError):
            Config.from_env(bad)


def item(title: str, age_days: float) -> Item:
    return Item(title, title, updated=datetime.now(UTC) - timedelta(days=age_days))


class FakeFetcher:
    def __init__(self, releases: list[Item] | Exception, commits: list[Item]) -> None:
        self._releases, self._commits = releases, commits

    def releases(self, repo: Repo) -> list[Item]:
        if isinstance(self._releases, Exception):
            raise self._releases
        return self._releases

    def commits(self, repo: Repo, branch: str = "") -> list[Item]:
        return self._commits


def app_with(fetcher: Any, **cfg: Any) -> App:
    return App(Config(discover=False, **cfg), docker=None, fetcher=fetcher)  # type: ignore[arg-type]


REPO = Repo("github.com", "a/b")


def test_collect_prefers_fresh_releases() -> None:
    a = app_with(FakeFetcher([item("v2", 3)], [item("c1", 1)]))
    items, kind = a.collect(Target(REPO, "test"))
    assert kind == "release" and items[0].title == "v2"


def test_collect_falls_back_on_stale_missing_or_filtered_releases() -> None:
    for releases in ([item("v1", 800)], [], NotFoundError("x")):
        items, kind = app_with(FakeFetcher(releases, [item("c1", 1)])).collect(Target(REPO, "test"))
        assert kind == "commit" and items[0].title == "c1"
    a = app_with(FakeFetcher([item("v2-rc1", 1)], [item("c1", 1)]), exclude=re.compile("rc"))
    assert a.collect(Target(REPO, "test"))[1] == "commit"


def test_collect_forced_modes() -> None:
    a = app_with(FakeFetcher([item("v1", 800)], [item("c1", 1)]))
    assert a.collect(Target(REPO, "test", mode="releases"))[1] == "release"
    assert a.collect(Target(REPO, "test", mode="commits"))[1] == "commit"
    with pytest.raises(ValueError):
        a.collect(Target(REPO, "test", mode="bogus"))


def test_targets_merge_ignore_and_unresolved() -> None:
    a = app_with(None)
    src = {"org.opencontainers.image.source": "https://github.com/tt-rss/tt-rss"}
    targets, unresolved, ignored = a.targets(
        [
            Container("ttrss_app", "x", src),
            Container("ttrss_web", "y", {**src, "dockfeed.include": "^v", "org.opencontainers.image.version": "1.2"}),
            Container("skip", "redis", {"dockfeed.enable": "false"}),
            Container("mystery", "someone/thing", {}),
        ]
    )
    t = targets["github.com/tt-rss/tt-rss"]
    assert t.containers == ["ttrss_app", "ttrss_web"]
    assert t.include is not None and t.running == "1.2"
    assert unresolved == {"mystery": "someone/thing"}
    assert ignored == ["skip"]


def test_feed_filters() -> None:
    class Docker:
        def containers(self, include_stopped: bool = False) -> list[Container]:
            return [Container("one", "ghcr.io/a/b", {})]

    a = App(Config(discover=False), docker=Docker(), fetcher=FakeFetcher([item("v1", 1)], []))  # type: ignore[arg-type]
    assert a.feed({}, "http://x/") is None
    a.refresh()
    assert a.status["repos"][0]["kind"] == "release"
    full = a.feed({}, "http://x/") or b""
    assert b"one: v1" in full
    assert b"one: v1" not in (a.feed({"container": ["other"]}, "http://x/") or b"")
    assert b"one: v1" not in (a.feed({"kind": ["commit"]}, "http://x/") or b"")
    assert time.time()  # keep the import used on every Python


def test_cap_applies_after_container_filter() -> None:
    class Docker:
        def containers(self, include_stopped: bool = False) -> list[Container]:
            return [Container("busy", "ghcr.io/a/busy", {}), Container("quiet", "ghcr.io/a/quiet", {})]

    class Fetcher(FakeFetcher):
        def releases(self, repo: Repo) -> list[Item]:
            if repo.name == "busy":
                return [item(f"b{i}", 1) for i in range(5)]
            return [item("q1", 30)]

    a = App(Config(discover=False, max_items=3), docker=Docker(), fetcher=Fetcher([], []))  # type: ignore[arg-type]
    a.refresh()
    assert b"quiet: q1" not in (a.feed({}, "http://x/") or b"")
    assert b"quiet: q1" in (a.feed({"container": ["quiet"]}, "http://x/") or b"")


class LabelDocker:
    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self.labels = labels or {}

    def hostname(self) -> str:
        return "Tower"

    def containers(self, include_stopped: bool = False) -> list[Container]:
        return [Container("one", "ghcr.io/a/b", self.labels)]


def summary_app(
    fetcher: FakeFetcher, labels: dict[str, str] | None = None, feed_type: str = "summary", **cfg: Any
) -> App:
    config = Config(discover=False, feed_type=feed_type, summary_period=100, **cfg)
    return App(config, docker=LabelDocker(labels), fetcher=fetcher)  # type: ignore[arg-type]


def test_summary_mode_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    f = FakeFetcher([item("v1", 1)], [])
    a = summary_app(f)
    a.refresh()
    assert b"<entry>" not in (a.feed({}, "http://x/") or b"")
    assert a.status["repos"][0]["feed"] == "summary" and a.status["repos"][0]["items"] == 0

    f._releases = [item("v3", 0), item("v2", 0.5), item("v1", 1)]
    a.refresh()
    assert a.status["repos"][0]["pending"] == 2 and "next_summary" in a.status["repos"][0]
    assert b"<entry>" not in (a.feed({}, "http://x/") or b"")

    now[0] += 100
    a.refresh()
    body = a.feed({}, "http://x/") or b""
    assert body.count(b"<entry>") == 1
    assert b"one (summary): 2 new releases, latest v3" in body
    assert b'term="summary"' in body and b'term="release"' in body and b"used by one on Tower" in body
    assert a.status["repos"][0]["pending"] == 0 and a.status["repos"][0]["items"] == 1
    assert b"<entry>" in (a.feed({"container": ["one"]}, "http://x/") or b"")
    assert b"<entry>" not in (a.feed({"kind": ["commit"]}, "http://x/") or b"")


def test_summary_counts_a_burst_past_items_per_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    f = FakeFetcher([], [])
    a = summary_app(f, {"dockfeed.mode": "releases"}, items_per_repo=1)
    a.refresh()
    f._releases = [item(f"v{i}", 1) for i in range(3)]
    a.refresh()
    assert a.status["repos"][0]["pending"] == 3


def test_summary_commits_keep_their_full_message(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    f = FakeFetcher([], [])
    a = summary_app(f, {"dockfeed.mode": "commits"})
    a.refresh()
    f._commits = [Item("c1", "fix: x", content="line one\nline two")]
    a.refresh()
    now[0] += 100
    a.refresh()
    body = a.feed({}, "http://x/") or b""
    assert b"1 new commit: fix: x" in body and b"line one\nline two" in body and b"white-space" in body


def test_failed_fetch_keeps_state_and_still_closes_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    f = FakeFetcher([item("v1", 1)], [])
    a = summary_app(f, {"dockfeed.mode": "releases"})
    a.refresh()
    f._releases = [item("v2", 0), item("v1", 1)]
    a.refresh()
    f._releases = OSError("down")
    now[0] += 100
    a.refresh()
    assert a.status["repos"][0]["error"] == "down"
    assert b"1 new release: v2" in (a.feed({}, "http://x/") or b"")
    f._releases = [item("v2", 0), item("v1", 1)]
    a.refresh()
    assert a.status["repos"][0]["pending"] == 0


def test_failed_first_fetch_does_not_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    f = FakeFetcher(OSError("down"), [])
    a = summary_app(f, {"dockfeed.mode": "releases"})
    a.refresh()
    f._releases = [item("v1", 1)]
    a.refresh()
    assert a.status["repos"][0]["pending"] == 0
    assert "error" not in a.status["repos"][0]


def test_label_overrides_feed_type_both_ways() -> None:
    a = summary_app(FakeFetcher([item("v1", 1)], []), {"dockfeed.feed": "direct"})
    a.refresh()
    assert a.status["repos"][0]["feed"] == "direct"
    assert b"one: v1" in (a.feed({}, "http://x/") or b"")

    a = summary_app(FakeFetcher([item("v1", 1)], []), {"dockfeed.feed": "summary"}, feed_type="direct")
    a.refresh()
    assert a.status["repos"][0]["feed"] == "summary"
    assert b"<entry>" not in (a.feed({}, "http://x/") or b"")


def test_invalid_feed_label_is_a_repo_error_and_keeps_state() -> None:
    d = LabelDocker()
    a = App(Config(discover=False, feed_type="summary"), docker=d, fetcher=FakeFetcher([item("v1", 1)], []))  # type: ignore[arg-type]
    a.refresh()
    known = set(a.summaries._states)
    d.labels = {"dockfeed.feed": "weekly"}
    a.refresh()
    assert "unknown dockfeed.feed 'weekly'" in a.status["repos"][0]["error"]
    assert set(a.summaries._states) == known and known


def test_switching_to_direct_drops_summary_state() -> None:
    d = LabelDocker()
    a = App(Config(discover=False, feed_type="summary"), docker=d, fetcher=FakeFetcher([item("v1", 1)], []))  # type: ignore[arg-type]
    a.refresh()
    assert a.summaries._states
    d.labels = {"dockfeed.feed": "direct"}
    a.refresh()
    assert not a.summaries._states


def test_summaries_persist_across_app_restarts(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    monkeypatch.setattr("dockfeed.app.time.time", lambda: now[0])
    a = summary_app(FakeFetcher([item("v1", 1)], []), data_dir=str(tmp_path))
    a.refresh()
    newer = [item("v2", 0), item("v1", 1)]
    a.fetcher._releases = newer  # type: ignore[attr-defined]
    a.refresh()

    b = summary_app(FakeFetcher(newer, []), data_dir=str(tmp_path))
    now[0] += 100
    b.refresh()
    assert b"1 new release: v2" in (b.feed({}, "http://x/") or b"")


def test_store_failure_shows_in_status(tmp_path: Any) -> None:
    a = summary_app(FakeFetcher([item("v1", 1)], []), data_dir=str(tmp_path / "missing"))
    a.refresh()
    assert "summaries.json" in a.status["summary_store_error"]
    assert a.feed({}, "http://x/") is not None


class HostDocker:
    def __init__(self, host: str | Exception) -> None:
        self.host = host

    def hostname(self) -> str:
        if isinstance(self.host, Exception):
            raise self.host
        return self.host

    def containers(self, include_stopped: bool = False) -> list[Container]:
        return [Container("rsshub", "ghcr.io/diygod/rsshub", {})]


def footer_host(cfg: Config, docker: HostDocker) -> tuple[str, bytes]:
    a = App(cfg, docker=docker, fetcher=FakeFetcher([item("v1", 1)], []))  # type: ignore[arg-type]
    a.refresh()
    return a.status["host"], a.feed({}, "http://x/") or b""


def test_host_from_engine_override_and_blocked_info() -> None:
    host, body = footer_host(Config(discover=False), HostDocker("Tower"))
    assert host == "Tower" and b"used by rsshub on Tower" in body
    host, body = footer_host(Config(discover=False, host_name="nas"), HostDocker("Tower"))
    assert host == "nas" and b"used by rsshub on nas" in body
    host, body = footer_host(Config(discover=False), HostDocker(OSError("403 Forbidden")))
    assert host == "" and b"used by rsshub on" not in body and b"used by rsshub" in body
    assert Config.from_env({"HOST_NAME": "nas"}).host_name == "nas"


@pytest.fixture
def server() -> Iterator[str]:
    a = app_with(None)
    a.ready = True
    srv = Server(("127.0.0.1", 0), make_handler(a))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def request(
    addr: str, method: str, path: str = "/healthz", body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(addr, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_server_only_serves_pages(server: str) -> None:
    assert request(server, "GET") == (200, b"ok\n")
    assert request(server, "HEAD") == (200, b"")
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"):
        assert request(server, method, body=b"x")[0] == 405
    assert request(server, "GET", body=b"x")[0] == 400
    assert request(server, "GET", headers={"Transfer-Encoding": "chunked"})[0] == 400
    assert request(server, "GET", "/healthz?" + "a" * 3000)[0] == 414


def test_self_link_ignores_hostile_host_headers(server: str) -> None:
    _, body = request(server, "GET", "/", headers={"Host": "feeds.lan:8080", "X-Forwarded-Proto": "https"})
    assert b'href="https://feeds.lan:8080/"' in body
    _, body = request(server, "GET", "/", headers={"Host": 'x"><script>', "X-Forwarded-Proto": "javascript"})
    assert b'href="http://localhost/"' in body and b"script" not in body
