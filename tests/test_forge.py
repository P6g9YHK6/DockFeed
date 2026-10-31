import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from dockfeed.feed import Item
from dockfeed.forge import Fetcher, Forge, NotFoundError
from dockfeed.source import Repo

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>v1</title>
<updated>2026-01-01T00:00:00Z</updated><link href="https://x/1"/></entry></feed>"""


class Recorder(Fetcher):
    """Records which URLs would be fetched instead of going to the network."""

    def __init__(self, json_by_url: dict[str, Any] | None = None) -> None:
        super().__init__("test")
        self.urls: list[str] = []
        self.json_by_url = json_by_url or {}

    def feed(self, url: str) -> list[Item]:
        self.urls.append(url)
        return []

    def get_json(self, url: str) -> Any:
        if url not in self.json_by_url:
            raise NotFoundError(url)
        return self.json_by_url[url]


def test_github_urls() -> None:
    f = Recorder()
    r = Repo("github.com", "a/b")
    f.releases(r)
    f.commits(r)
    f.commits(r, "dev")
    assert f.urls == [
        "https://github.com/a/b/releases.atom",
        "https://github.com/a/b/commits.atom",
        "https://github.com/a/b/commits/dev.atom",
    ]


def test_gitea_and_gitlab_urls_use_default_branch() -> None:
    f = Recorder(
        {
            "https://codeberg.org/api/v1/repos/forgejo/forgejo": {"default_branch": "forgejo"},
            "https://gitlab.com/api/v4/projects/g%2Fsub%2Fp": {"default_branch": "main"},
        }
    )
    f.releases(Repo("codeberg.org", "forgejo/forgejo"))
    f.commits(Repo("codeberg.org", "forgejo/forgejo"))
    f.releases(Repo("gitlab.com", "g/sub/p"))
    f.commits(Repo("gitlab.com", "g/sub/p"))
    assert f.urls == [
        "https://codeberg.org/forgejo/forgejo/releases.rss",
        "https://codeberg.org/forgejo/forgejo/rss/branch/forgejo",
        "https://gitlab.com/g/sub/p/-/tags?format=atom",
        "https://gitlab.com/g/sub/p/-/commits/main?format=atom",
    ]


def test_kind_probes_self_hosted_once() -> None:
    f = Recorder({"https://git.example.org/api/v1/version": {"version": "1.22"}})
    assert f.kind(Repo("git.example.org", "a/b")) == Forge.GITEA
    f.json_by_url.clear()
    assert f.kind(Repo("git.example.org", "c/d")) == Forge.GITEA
    g = Recorder({"https://mau.dev/api/v4/projects/mautrix%2Fsignal": {"id": 5}})
    assert g.kind(Repo("mau.dev", "mautrix/signal")) == Forge.GITLAB
    assert Recorder().kind(Repo("nowhere.example", "a/b")) == Forge.UNKNOWN
    with pytest.raises(ValueError):
        Recorder().releases(Repo("nowhere.example", "a/b"))


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v


def test_feed_sends_validators_and_reuses_on_304(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> FakeResponse:
        seen.append(req)
        if req.get_header("If-none-match") == '"v1"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", Message(), None)
        return FakeResponse(ATOM, {"ETag": '"v1"'})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    f = Fetcher("test")
    first = f.feed("https://github.com/a/b/releases.atom")
    second = f.feed("https://github.com/a/b/releases.atom")
    assert [i.title for i in first] == ["v1"] and second == first
    assert seen[1].get_header("If-none-match") == '"v1"'


def test_feed_404_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    auth: list[str | None] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> FakeResponse:
        auth.append(req.get_header("Authorization"))
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    f = Fetcher("test", github_token="t0ken")
    with pytest.raises(NotFoundError):
        f.feed("https://github.com/a/b/releases.atom")
    with pytest.raises(NotFoundError):
        f.get_json("https://api.github.com/repos/a/b")
    assert auth == [None, "Bearer t0ken"]


def test_exists_caches_answers_but_not_outages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> FakeResponse:
        calls.append(req.full_url)
        if "down" in req.full_url:
            raise urllib.error.URLError("timeout")
        if "missing" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", Message(), None)
        return FakeResponse(b"", {})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    f = Fetcher("test")
    for _ in range(2):
        assert f.exists(Repo("github.com", "a/real"))
        assert not f.exists(Repo("github.com", "a/missing"))
        assert not f.exists(Repo("github.com", "a/down"))
    assert calls.count("https://github.com/a/real") == 1
    assert calls.count("https://github.com/a/missing") == 1
    assert calls.count("https://github.com/a/down") == 2
