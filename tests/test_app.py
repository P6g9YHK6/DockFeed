import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dockfeed.app import App, Config, Target, parse_duration
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
