"""Talking to GitHub, GitLab, Gitea and Forgejo over their public feeds and APIs."""

from __future__ import annotations

import enum
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .feed import Item, parse_feed
from .source import Repo

TIMEOUT = 30


class NotFoundError(Exception):
    pass


class Forge(enum.Enum):
    UNKNOWN = "unknown"
    GITHUB = "github"
    GITEA = "gitea"  # also Forgejo and Codeberg
    GITLAB = "gitlab"


KNOWN_HOSTS = {
    "github.com": Forge.GITHUB,
    "gitlab.com": Forge.GITLAB,
    "codeberg.org": Forge.GITEA,
    "code.forgejo.org": Forge.GITEA,
    "gitea.com": Forge.GITEA,
}


@dataclass
class _Cached:
    etag: str
    last_modified: str
    items: list[Item]


@dataclass
class GitHubRepoInfo:
    full_name: str
    archived: bool
    stars: int


class Fetcher:
    """Fetches feeds and remembers validators so an unchanged feed costs a 304."""

    def __init__(self, user_agent: str, github_token: str = "") -> None:
        self.user_agent = user_agent
        self.github_token = github_token
        self._lock = threading.Lock()
        self._feeds: dict[str, _Cached] = {}
        self._kinds: dict[str, Forge] = {}
        self._branches: dict[str, str] = {}
        self._known: dict[str, bool] = {}

    def _request(self, url: str, headers: dict[str, str] | None = None, method: str = "GET") -> urllib.request.Request:
        req = urllib.request.Request(url, method=method, headers=headers or {})
        req.add_header("User-Agent", self.user_agent)
        if self.github_token and req.host == "api.github.com":
            req.add_header("Authorization", "Bearer " + self.github_token)
        return req

    def feed(self, url: str) -> list[Item]:
        with self._lock:
            prev = self._feeds.get(url)
        headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.1"}
        if prev:
            if prev.etag:
                headers["If-None-Match"] = prev.etag
            if prev.last_modified:
                headers["If-Modified-Since"] = prev.last_modified
        try:
            with urllib.request.urlopen(self._request(url, headers), timeout=TIMEOUT) as resp:
                data = resp.read()
                etag, modified = resp.headers.get("ETag", ""), resp.headers.get("Last-Modified", "")
        except urllib.error.HTTPError as e:
            if e.code == 304 and prev:
                return prev.items
            if e.code == 404:
                raise NotFoundError(url) from e
            raise
        items = parse_feed(data)
        with self._lock:
            self._feeds[url] = _Cached(etag, modified, items)
        return items

    def get_json(self, url: str) -> Any:
        try:
            with urllib.request.urlopen(self._request(url, {"Accept": "application/json"}), timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFoundError(url) from e
            raise

    def exists(self, repo: Repo) -> bool:
        """Whether a guessed repo is real, remembered for the process lifetime."""
        key = str(repo)
        with self._lock:
            if key in self._known:
                return self._known[key]
        try:
            with urllib.request.urlopen(self._request(repo.url, method="HEAD"), timeout=TIMEOUT) as resp:
                ok = bool(resp.status == 200)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False
            ok = False
        except OSError:
            # Network trouble is not an answer; try again next scan.
            return False
        with self._lock:
            self._known[key] = ok
        return ok

    def kind(self, repo: Repo) -> Forge:
        """Identify the forge software behind a host, probing self-hosted instances once."""
        if repo.host in KNOWN_HOSTS:
            return KNOWN_HOSTS[repo.host]
        with self._lock:
            if repo.host in self._kinds:
                return self._kinds[repo.host]
        if self._probe(f"https://{repo.host}/api/v1/version", "version"):
            k = Forge.GITEA
        elif self._probe(f"https://{repo.host}/api/v4/projects/{quote(repo.path, safe='')}", "id"):
            k = Forge.GITLAB
        else:
            k = Forge.UNKNOWN
        with self._lock:
            self._kinds[repo.host] = k
        return k

    def _probe(self, url: str, key: str) -> bool:
        try:
            d = self.get_json(url)
        except (OSError, ValueError, NotFoundError):
            return False
        return isinstance(d, dict) and bool(d.get(key))

    def default_branch(self, repo: Repo, kind: Forge) -> str:
        key = str(repo)
        with self._lock:
            if key in self._branches:
                return self._branches[key]
        if kind == Forge.GITEA:
            url = f"https://{repo.host}/api/v1/repos/{repo.path}"
        elif kind == Forge.GITLAB:
            url = f"https://{repo.host}/api/v4/projects/{quote(repo.path, safe='')}"
        else:
            raise ValueError(f"no branch lookup for {repo}")
        branch = str(self.get_json(url).get("default_branch") or "")
        if not branch:
            raise ValueError(f"{repo}: empty default branch")
        with self._lock:
            self._branches[key] = branch
        return branch

    def releases(self, repo: Repo) -> list[Item]:
        """The repo's releases. GitHub and GitLab list tags here when there are no releases."""
        kind = self.kind(repo)
        if kind == Forge.GITHUB:
            return self.feed(repo.url + "/releases.atom")
        if kind == Forge.GITEA:
            return self.feed(repo.url + "/releases.rss")
        if kind == Forge.GITLAB:
            return self.feed(repo.url + "/-/tags?format=atom")
        raise ValueError(f"{repo.host}: unrecognized forge")

    def commits(self, repo: Repo, branch: str = "") -> list[Item]:
        """Recent commits on branch, or on the default branch when branch is empty."""
        kind = self.kind(repo)
        if kind == Forge.GITHUB:
            if not branch:
                return self.feed(repo.url + "/commits.atom")
            return self.feed(f"{repo.url}/commits/{quote(branch, safe='')}.atom")
        if kind == Forge.UNKNOWN:
            raise ValueError(f"{repo.host}: unrecognized forge")
        branch = branch or self.default_branch(repo, kind)
        if kind == Forge.GITEA:
            return self.feed(f"{repo.url}/rss/branch/{quote(branch, safe='')}")
        return self.feed(f"{repo.url}/-/commits/{quote(branch, safe='')}?format=atom")

    def github_repo(self, path: str) -> GitHubRepoInfo:
        d = self.get_json("https://api.github.com/repos/" + path)
        return GitHubRepoInfo(d["full_name"], bool(d.get("archived")), int(d.get("stargazers_count", 0)))

    def github_search(self, q: str) -> list[GitHubRepoInfo]:
        d = self.get_json("https://api.github.com/search/repositories?per_page=30&q=" + quote(q))
        return [
            GitHubRepoInfo(i["full_name"], bool(i.get("archived")), int(i.get("stargazers_count", 0)))
            for i in d.get("items", [])
        ]
