"""Matching a container to the repository its image is built from, using only local information."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

LABEL_PREFIX = "dockfeed."


@dataclass(frozen=True)
class Repo:
    """A source repository on a forge, e.g. host github.com and path immich-app/immich."""

    host: str
    path: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.path}"

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def owner(self) -> str:
        return self.path.split("/", 1)[0]

    def __str__(self) -> str:
        return f"{self.host}/{self.path}"


@dataclass
class Container:
    name: str
    image: str
    labels: dict[str, str]


# Docker Hub library images carry no OCI source label, so without this most
# databases and base images would go unresolved.
OFFICIAL_IMAGES = {
    "alpine": "https://github.com/alpinelinux/docker-alpine",
    "caddy": "https://github.com/caddyserver/caddy",
    "docker": "https://github.com/moby/moby",
    "elasticsearch": "https://github.com/elastic/elasticsearch",
    "ghost": "https://github.com/TryGhost/Ghost",
    "golang": "https://github.com/golang/go",
    "haproxy": "https://github.com/haproxy/haproxy",
    "httpd": "https://github.com/apache/httpd",
    "mariadb": "https://github.com/MariaDB/server",
    "memcached": "https://github.com/memcached/memcached",
    "mongo": "https://github.com/mongodb/mongo",
    "mysql": "https://github.com/mysql/mysql-server",
    "nextcloud": "https://github.com/nextcloud/server",
    "nginx": "https://github.com/nginx/nginx",
    "node": "https://github.com/nodejs/node",
    "php": "https://github.com/php/php-src",
    "postgres": "https://github.com/postgres/postgres",
    "python": "https://github.com/python/cpython",
    "rabbitmq": "https://github.com/rabbitmq/rabbitmq-server",
    "redis": "https://github.com/redis/redis",
    "registry": "https://github.com/distribution/distribution",
    "traefik": "https://github.com/traefik/traefik",
    "valkey": "https://github.com/valkey-io/valkey",
    "wordpress": "https://github.com/WordPress/WordPress",
}

# Hosts that serve repositories directly, so an image.url label pointing there is a usable source.
KNOWN_FORGES = {"github.com", "gitlab.com", "codeberg.org", "code.forgejo.org", "gitea.com"}

_SCP_LIKE = re.compile(r"^git@([^:]+):(.+)$")


def parse_repo_url(raw: str | None) -> Repo | None:
    """Normalize the many spellings found in image labels into a Repo."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if m := _SCP_LIKE.match(raw):
        raw = f"https://{m[1]}/{m[2]}"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlsplit(raw)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    path = u.path.strip("/").removesuffix(".git")
    # Drop GitLab deep links like /-/tree/main down to the project.
    path = path.split("/-/", 1)[0]
    parts = path.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    if host != "gitlab.com":
        parts = parts[:2]
    return Repo(host, "/".join(parts))


def split_image(image: str) -> tuple[str, str, str]:
    """Return registry, repository path and tag of an image reference."""
    image = image.split("@", 1)[0]
    tag = ""
    colon = image.rfind(":")
    if colon > image.rfind("/"):
        image, tag = image[:colon], image[colon + 1 :]
    first, sep, rest = image.partition("/")
    if sep and (any(c in first for c in ".:") or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", image
    if registry in ("docker.io", "index.docker.io"):
        registry = "docker.io"
        path = path.removeprefix("library/")
    return registry, path, tag


def resolve_repo(c: Container) -> tuple[Repo, str] | None:
    """Work out where a container's code lives from its labels and image name, most explicit hint first."""
    labels = c.labels
    if v := labels.get(LABEL_PREFIX + "source"):
        r = parse_repo_url(v)
        return (r, f"label {LABEL_PREFIX}source") if r else None
    for label in ("org.opencontainers.image.source", "org.label-schema.vcs-url"):
        if r := parse_repo_url(labels.get(label)):
            return r, label
    for label in ("org.opencontainers.image.url", "org.label-schema.url"):
        r = parse_repo_url(labels.get(label))
        if r and r.host in KNOWN_FORGES:
            return r, label

    registry, path, _ = split_image(c.image)
    candidate: tuple[str, str] | None = None
    if registry == "ghcr.io":
        candidate = ("https://github.com/" + path, "ghcr.io image name")
    elif registry in ("codeberg.org", "code.forgejo.org", "data.forgejo.org"):
        host = "code.forgejo.org" if registry == "data.forgejo.org" else registry
        candidate = (f"https://{host}/{path}", f"{registry} image name")
    elif registry == "registry.gitlab.com":
        candidate = ("https://gitlab.com/" + path, "registry.gitlab.com image name")
    elif registry == "lscr.io" or (registry == "docker.io" and path.startswith("linuxserver/")):
        name = path.removeprefix("linuxserver/")
        candidate = (f"https://github.com/linuxserver/docker-{name}", "linuxserver image name")
    elif registry == "docker.io" and "/" not in path and path in OFFICIAL_IMAGES:
        candidate = (OFFICIAL_IMAGES[path], "official image map")
    if candidate and (r := parse_repo_url(candidate[0])):
        return r, candidate[1]
    return None


def guess_repos(image: str) -> list[Repo]:
    """Likely GitHub repos for a Docker Hub image named owner/name. Callers must check they exist."""
    registry, path, _ = split_image(image)
    owner, sep, name = path.partition("/")
    if registry != "docker.io" or not sep or "/" in name:
        return []
    repos = [Repo("github.com", f"{owner}/{name}")]
    if not name.startswith("docker-"):
        repos.append(Repo("github.com", f"{owner}/docker-{name}"))
    return repos
