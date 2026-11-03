from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dockfeed.feed import Item
from dockfeed.source import Repo
from dockfeed.summary import Schedule, held, summarize

REPO = Repo("github.com", "a/b")
DAILY = Schedule(86400, 7 * 3600)
CUTOFF = datetime(2026, 11, 3, 7, tzinfo=UTC)
NOW = (CUTOFF + timedelta(minutes=30)).timestamp()


def at(hours: float, title: str = "v") -> Item:
    return Item(title, title, f"https://x/{title}", CUTOFF + timedelta(hours=hours))


def digests(items: list[Item], keep: int = 10, now: float = NOW, kind: str = "release") -> list[Item]:
    return summarize(REPO, kind, items, now, DAILY, keep)


ZURICH = ZoneInfo("Europe/Zurich")


def utc(when: str) -> float:
    return datetime.fromisoformat(when).replace(tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    ("schedule", "when", "want"),
    [
        (Schedule(86400), "2026-11-03T12:00", "2026-11-03T00:00"),
        (DAILY, "2026-11-03T07:00", "2026-11-03T07:00"),
        (DAILY, "2026-11-03T06:59", "2026-11-02T07:00"),
        (Schedule(6 * 3600, 7 * 3600), "2026-11-03T02:00", "2026-11-03T01:00"),
        # 07:00 in Zurich, which is 06:00 UTC in winter and 05:00 UTC in summer.
        (Schedule(86400, 7 * 3600, ZURICH), "2026-11-03T12:00", "2026-11-03T06:00"),
        (Schedule(86400, 7 * 3600, ZURICH), "2026-07-03T05:30", "2026-07-03T05:00"),
        (Schedule(86400, 7 * 3600, ZURICH), "2026-07-03T04:30", "2026-07-02T05:00"),
        # 02:30 happens twice on 2026-10-25: the cutoff is the first, and the repeat doesn't undo it.
        (Schedule(86400, 2.5 * 3600, ZURICH), "2026-10-25T00:30", "2026-10-25T00:30"),
        (Schedule(86400, 2.5 * 3600, ZURICH), "2026-10-25T01:10", "2026-10-25T00:30"),
        # 02:30 doesn't exist on 2026-03-29: the cutoff falls at 03:30 instead.
        (Schedule(86400, 2.5 * 3600, ZURICH), "2026-03-29T01:00", "2026-03-28T01:30"),
        (Schedule(86400, 2.5 * 3600, ZURICH), "2026-03-29T01:30", "2026-03-29T01:30"),
    ],
)
def test_last_cutoff(schedule: Schedule, when: str, want: str) -> None:
    assert schedule.at(schedule.index(utc(when))) == utc(want)


@pytest.mark.parametrize("day", ["2026-03-28T00:00", "2026-10-24T00:00"])
@pytest.mark.parametrize("hours", [0, 2, 2.5, 3, 7])
def test_cutoffs_never_go_backwards_across_dst(day: str, hours: float) -> None:
    s = Schedule(86400, hours * 3600, ZURICH)
    seen = [s.index(utc(day) + m * 600) for m in range(48 * 6)]
    assert seen == sorted(seen)
    assert seen[-1] - seen[0] in (1, 2)
    for k in set(seen):
        assert s.at(k) < s.at(k + 1)


def test_items_since_the_cutoff_are_held_back() -> None:
    items = [at(0.2, "new"), at(0, "on the dot"), at(-1, "old")]
    (d,) = digests(items)
    assert d.title == "1 new release: old"
    assert held(items, NOW, DAILY) == 2


def test_nothing_new_means_no_digest() -> None:
    assert digests([]) == []
    assert digests([at(0.2)]) == []


def test_digest_is_stable_and_dated_at_the_cutoff() -> None:
    items = [at(-1, "v2"), at(-2, "v1")]
    (d,) = digests(items)
    assert d.title == "2 new releases, latest v2"
    assert d.updated == CUTOFF
    assert d.id == f"urn:dockfeed:summary:github.com/a/b:{int(CUTOFF.timestamp())}"
    assert digests(items, now=NOW + 3600) == [d]


def test_each_window_gets_its_own_digest_newest_first() -> None:
    items = [at(-1, "b"), at(-23, "a"), at(-25, "older"), at(-24 * 3, "oldest")]
    ds = digests(items)
    assert [d.title for d in ds] == ["2 new releases, latest b", "1 new release: older", "1 new release: oldest"]
    assert [d.updated for d in ds] == [CUTOFF, CUTOFF - timedelta(days=1), CUTOFF - timedelta(days=2)]


def test_only_the_last_periods_are_kept() -> None:
    items = [at(-1, "a"), at(-25, "b"), at(-49, "c")]
    assert [d.title for d in digests(items, keep=2)] == ["1 new release: a", "1 new release: b"]


def test_commits_are_named_and_order_does_not_depend_on_input() -> None:
    (d,) = digests([at(-3, "older"), at(-1, "fix: x\n\nbody")], kind="commit")
    assert d.title == "2 new commits, latest fix: x"
    assert d.content.index("fix: x") < d.content.index("older")


def test_digest_body_is_escaped_and_keeps_the_notes() -> None:
    when = CUTOFF - timedelta(hours=1)
    evil = Item("e", "<b>x</b>\nmore", 'https://x/"><script>', when, "a<b", "<p>notes</p>")
    (d,) = digests([evil])
    assert "&lt;b&gt;x&lt;/b&gt;" in d.content and "<script>" not in d.content
    assert "a&lt;b" in d.content and "<p>notes</p>" in d.content and "more" not in d.content


def test_links() -> None:
    assert digests([at(-1, "one")])[0].link == "https://x/one"
    assert digests([at(-1, "a"), at(-2, "b")])[0].link == REPO.url


def test_digest_dates_follow_the_time_zone() -> None:
    s = Schedule(86400, 7 * 3600, ZURICH)
    item = Item("v", "v", updated=datetime(2026, 11, 3, 5, tzinfo=UTC), author="me")
    (d,) = summarize(REPO, "release", [item], utc("2026-11-03T12:00"), s, 10)
    assert "2026-11-03 06:00 CET" in d.content
    assert d.updated.timestamp() == utc("2026-11-03T06:00")
