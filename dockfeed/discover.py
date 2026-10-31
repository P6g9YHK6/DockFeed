"""Finding repos for images that carry no usable labels, using network lookups.

Every candidate is verified before use, and answers are cached so lookups don't repeat each scan.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from .forge import Fetcher, Forge, NotFoundError
from .source import Repo, guess_repos, parse_repo_url, split_image

MISS_TTL = 24 * 3600

# Repos that show up in Docker Hub READMEs without being the image's upstream.
HUB_LINK_NOISE = re.compile(r"^github\.com/(docker-library|docker|moby|library|opencontainers)/", re.I)
FORGE_LINK = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|codeberg\.org)/[\w.-]+/[\w.-]+")
BIG_REGISTRIES = {"docker.io", "ghcr.io", "lscr.io", "quay.io", "gcr.io", "mcr.microsoft.com", "public.ecr.aws"}


class TransientError(Exception):
    """A lookup failed for reasons that say nothing about the answer, like a rate limit."""


@dataclass
class _Discovery:
    repo: Repo | None
    via: str
    at: float


@dataclass
class Candidate:
    repo: Repo
    name: bool = False
    owner: bool = False
    archived: bool = False
    stars: int = 0
    mentions: int = 0

    def score(self) -> int:
        return 4 * self.name + 2 * self.owner + (not self.archived)


def rank(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (c.score(), c.stars, c.mentions), reverse=True)


def strip_docker(s: str) -> str:
    return s.lower().removeprefix("docker-").removesuffix("-docker")


def name_match(repo: str, image: str) -> bool:
    """Equal names, or one being the other plus a dash suffix (portainer-ee runs portainer)."""
    a, b = strip_docker(repo), strip_docker(image)
    return a == b or b.startswith(a + "-") or a.startswith(b + "-")


def owner_match(forge_owner: str, ns: str) -> bool:
    """The Hub namespace as a prefix of the forge owner or the reverse (umap and umap-project)."""
    a, b = forge_owner.lower(), ns.lower()
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    return a.startswith(b) or b.startswith(a)


Step = Callable[[str, str], Repo | None]


class Discoverer:
    def __init__(self, fetcher: Fetcher) -> None:
        self.f = fetcher
        self._lock = threading.Lock()
        self._cache: dict[str, _Discovery] = {}
        self.steps: list[tuple[str, Step]] = [
            ("forge next to the registry", self.registry_sibling),
            ("guessed from Docker Hub name", self.hub_guess),
            ("Docker Hub description", self.hub_links),
            ("GitHub search", self.github_search),
        ]

    def discover(self, image: str) -> tuple[Repo, str] | None:
        registry, path, _ = split_image(image)
        key = f"{registry}/{path}"
        with self._lock:
            c = self._cache.get(key)
        if c and (c.repo or time.monotonic() - c.at < MISS_TTL):
            return (c.repo, c.via) if c.repo else None

        transient = False
        for via, step in self.steps:
            try:
                repo = step(registry, path)
            except (TransientError, OSError, ValueError):
                # Rate limits and outages aren't a "no"; leave the image uncached so the next scan retries.
                transient = True
                continue
            if repo:
                self._store(key, _Discovery(repo, via, time.monotonic()))
                return repo, via
        if not transient:
            self._store(key, _Discovery(None, "", time.monotonic()))
        return None

    def _store(self, key: str, d: _Discovery) -> None:
        with self._lock:
            self._cache[key] = d

    def registry_sibling(self, registry: str, path: str) -> Repo | None:
        """Self-hosted registries that sit next to their forge, like dock.mau.dev and mau.dev."""
        if registry in BIG_REGISTRIES:
            return None
        host = registry.split(":", 1)[0]
        hosts = [host]
        if len(labels := host.split(".")) > 2:
            hosts.append(".".join(labels[1:]))
        for h in hosts:
            r = parse_repo_url(f"https://{h}/{path}")
            if r and self.f.kind(r) != Forge.UNKNOWN and self.f.exists(r):
                return r
        return None

    def hub_guess(self, registry: str, path: str) -> Repo | None:
        for g in guess_repos(f"{registry}/{path}"):
            if self.f.exists(g):
                return g
        return None

    def hub_links(self, registry: str, path: str) -> Repo | None:
        ns, sep, name = path.partition("/")
        if registry != "docker.io" or not sep:
            return None
        try:
            hub = self.f.get_json(f"https://hub.docker.com/v2/repositories/{ns}/{name}/")
        except NotFoundError:
            return None
        text = f"{hub.get('description') or ''}\n{hub.get('full_description') or ''}"
        counts: Counter[Repo] = Counter()
        for link in FORGE_LINK.findall(text):
            r = parse_repo_url(link)
            if not r or HUB_LINK_NOISE.match(str(r)):
                continue
            if name_match(r.name, name) or owner_match(r.owner, ns):
                counts[r] += 1
        cands = []
        for r, n in counts.items():
            c = Candidate(r, name=name_match(r.name, name), owner=owner_match(r.owner, ns), mentions=n)
            if r.host == "github.com":
                try:
                    info = self.f.github_repo(r.path)
                except NotFoundError:
                    continue
                c.repo, c.stars, c.archived = Repo("github.com", info.full_name), info.stars, info.archived
            elif not self.f.exists(r):
                continue
            cands.append(c)
        return rank(cands)[0].repo if cands else None

    def github_search(self, registry: str, path: str) -> Repo | None:
        """Only accept a hit whose owner and name both line up with the image,
        so a popular but unrelated repo with the same name is never picked."""
        ns, sep, name = path.partition("/")
        if registry != "docker.io" or not sep:
            return None
        terms = [t for t in re.split(r"[-_.]", strip_docker(name)) if t]
        if not terms:
            return None
        cands = []
        for h in self.f.github_search(terms[0] + " in:name"):
            r = Repo("github.com", h.full_name)
            if name_match(r.name, name) and owner_match(r.owner, ns):
                cands.append(Candidate(r, name=True, owner=True, stars=h.stars, archived=h.archived))
        return rank(cands)[0].repo if cands else None
