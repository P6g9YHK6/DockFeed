import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dockfeed.feed import Item
from dockfeed.source import Repo
from dockfeed.summary import FILENAME, MAX_DIGEST_ITEMS, MAX_SEEN, SummaryStore

REPO = Repo("github.com", "a/b")
PERIOD = 100.0


def item(name: str, when: float = 0, **kw: str) -> Item:
    return Item(name, name, f"https://x/{name}", datetime.fromtimestamp(when, UTC), **kw)


def store(path: Path | str = "", keep: int = 10) -> SummaryStore:
    return SummaryStore(str(path), PERIOD, keep)


def scan(s: SummaryStore, items: list[Item], now: float, kind: str = "release") -> list[str]:
    s.observe(REPO, kind, items, now)
    s.flush(REPO, now)
    return [d.item.title for d in s.digests(REPO)]


def test_baseline_publishes_nothing() -> None:
    s = store()
    assert scan(s, [item("v1"), item("v2")], 0) == []
    assert scan(s, [item("v1"), item("v2")], 1000) == []
    assert s.pending(REPO) == (0, None)


def test_one_digest_per_period_however_many_scans() -> None:
    s = store()
    scan(s, [item("v1")], 0)
    assert scan(s, [item("v2", 10), item("v1")], 10) == []
    assert s.pending(REPO) == (1, 110)
    assert scan(s, [item("v3", 50), item("v2", 10), item("v1")], 50) == []
    assert scan(s, [item("v3", 50), item("v2", 10), item("v1")], 109) == []
    assert scan(s, [item("v4", 110), item("v3", 50), item("v2", 10)], 110) == ["3 new releases, latest v4"]
    assert s.pending(REPO) == (0, None)
    assert len(scan(s, [item("v4", 110), item("v3", 50)], 5000)) == 1


def test_update_after_close_opens_a_new_window() -> None:
    s = store()
    scan(s, [], 0)
    assert scan(s, [item("v1", 1)], 1) == []
    assert scan(s, [item("v1", 1)], 101) == ["1 new release: v1"]
    assert scan(s, [item("v2", 150), item("v1", 1)], 150) == ["1 new release: v1"]
    titles = scan(s, [item("v2", 150), item("v1", 1)], 250)
    assert titles == ["1 new release: v2", "1 new release: v1"]
    ids = [d.item.id for d in s.digests(REPO)]
    assert ids == ["urn:dockfeed:summary:github.com/a/b:150", "urn:dockfeed:summary:github.com/a/b:1"]


def test_edited_release_does_not_count_again() -> None:
    s = store()
    scan(s, [item("v1")], 0)
    assert scan(s, [Item("v1", "v1 renamed", updated=datetime.fromtimestamp(50, UTC))], 50) == []
    assert s.pending(REPO) == (0, None)


def test_kind_flip_rebaselines() -> None:
    s = store()
    scan(s, [item("v1")], 0)
    assert scan(s, [item("c1"), item("c2")], 10, kind="commit") == []
    assert s.pending(REPO) == (0, None)
    scan(s, [item("c3", 20), item("c1"), item("c2")], 20, kind="commit")
    assert s.pending(REPO)[0] == 1
    s.flush(REPO, 120)
    (d,) = s.digests(REPO)
    assert d.kind == "commit" and d.item.title == "1 new commit: c3"


def test_digest_body_is_newest_first_and_escaped() -> None:
    s = store()
    scan(s, [], 0)
    evil = Item("e", "<b>x</b>\nmore", 'https://x/"><script>', datetime.fromtimestamp(5, UTC), "a<b", "<p>notes</p>")
    scan(s, [item("v1", 1), evil], 5)
    s.flush(REPO, 105)
    (d,) = s.digests(REPO)
    body = d.item.content
    assert body.index("&lt;b&gt;x&lt;/b&gt;") < body.index(">v1</a>")
    assert "<script>" not in body and "a&lt;b" in body and "<p>notes</p>" in body and "more" not in body
    assert d.item.link == REPO.url
    assert d.item.updated == datetime.fromtimestamp(105, UTC)


def test_single_update_links_to_itself() -> None:
    s = store()
    scan(s, [], 0)
    scan(s, [item("v1", 1)], 1)
    s.flush(REPO, 101)
    assert s.digests(REPO)[0].item.link == "https://x/v1"


def test_digest_cap_says_how_many_more() -> None:
    s = store()
    scan(s, [], 0)
    n = MAX_DIGEST_ITEMS + 5
    scan(s, [item(f"v{i}", i) for i in range(n)], 1000)
    assert s.pending(REPO)[0] == n
    s.flush(REPO, 1100)
    (d,) = s.digests(REPO)
    assert d.item.title.startswith(f"{n} new releases")
    assert "and 5 more" in d.item.content and d.item.content.count("<h3>") == MAX_DIGEST_ITEMS


def test_digests_are_capped_per_repo() -> None:
    s = store(keep=2)
    scan(s, [], 0)
    for i in range(1, 5):
        now = i * 1000.0
        scan(s, [item(f"v{i}", now)], now)
        s.flush(REPO, now + PERIOD)
    assert [d.item.title for d in s.digests(REPO)] == ["1 new release: v4", "1 new release: v3"]


def test_seen_is_capped_but_keeps_the_current_fetch() -> None:
    s = store()
    scan(s, [item(f"old{i}") for i in range(MAX_SEEN)], 0)
    current = [item("a"), item("b")]
    scan(s, current, 1)
    scan(s, current, 2)
    assert s.pending(REPO)[0] == 2
    assert len(s._states[str(REPO)].seen) == MAX_SEEN


def test_retain_drops_direct_and_long_gone_repos() -> None:
    s = store()
    other = Repo("github.com", "c/d")
    s.observe(REPO, "release", [], 0)
    s.observe(other, "release", [], 0)
    s.retain([other], 10)
    assert str(other) not in s._states and str(REPO) in s._states
    s.retain([], 29 * 86400)
    assert str(REPO) in s._states
    s.retain([], 31 * 86400)
    assert str(REPO) not in s._states


def test_state_survives_a_reload_and_an_overdue_window_closes(tmp_path: Path) -> None:
    s = store(tmp_path)
    scan(s, [], 0)
    scan(s, [item("v1", 1, content="<p>a</p>")], 1)
    s.flush(REPO, 101)
    scan(s, [item("v2", 200), item("v1", 1)], 200)
    s.save()
    assert not (tmp_path / (FILENAME + ".tmp")).exists()

    again = store(tmp_path)
    again.load()
    assert again.pending(REPO) == (1, 300)
    assert [d.item.title for d in again.digests(REPO)] == ["1 new release: v1"]
    assert "<p>a</p>" in again.digests(REPO)[0].item.content
    assert scan(again, [item("v2", 200), item("v1", 1)], 9000)[0] == "1 new release: v2"


def test_save_only_writes_when_something_changed(tmp_path: Path) -> None:
    s = store(tmp_path)
    scan(s, [item("v1")], 0)
    s.save()
    path = tmp_path / FILENAME
    path.unlink()
    scan(s, [item("v1")], 1)
    s.save()
    assert not path.exists()
    scan(s, [item("v2", 2), item("v1")], 2)
    s.save()
    assert path.exists()


def test_memory_only_store_never_touches_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = store()
    s.load()
    scan(s, [item("v1")], 0)
    s.save()
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("body", ["{nope", "[]", '{"version": 99, "repos": {}}', '{"version": 1, "repos": []}'])
def test_corrupt_file_is_moved_aside(tmp_path: Path, body: str) -> None:
    path = tmp_path / FILENAME
    path.write_text(body)
    s = store(tmp_path)
    s.load()
    assert not s.error and not path.exists()
    assert (tmp_path / (FILENAME + ".corrupt")).read_text() == body
    scan(s, [item("v1")], 0)
    s.save()
    assert json.loads(path.read_text())["version"] == 1


def test_unwritable_dir_keeps_working_and_recovers(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    s = store(missing)
    scan(s, [], 0)
    scan(s, [item("v1", 1)], 1)
    s.save()
    assert "write" in s.error and s.pending(REPO)[0] == 1
    missing.mkdir()
    s.save()
    assert not s.error and (missing / FILENAME).exists()


def test_unreadable_file_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / FILENAME
    path.write_text("precious")
    real_open = open

    def denied(file: str, *a: str, **kw: str) -> object:
        if file == str(path):
            raise PermissionError("denied")
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", denied)
    s = store(tmp_path)
    s.load()
    monkeypatch.undo()
    assert "read" in s.error
    scan(s, [item("v1")], 0)
    s.save()
    assert path.read_text() == "precious"
