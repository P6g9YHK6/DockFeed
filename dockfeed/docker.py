"""A minimal Docker Engine API client: two read-only calls over the socket or TCP."""

from __future__ import annotations

import http.client
import json
import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

from .source import Container

TIMEOUT = 30
MAX_BODY = 64 << 20

# Every Engine API call DockFeed makes. Anything else is refused before it reaches the
# socket, so the client can't be talked into creating, exec'ing or deleting anything.
_ALLOWED = re.compile(r"/info|/containers/json\?all=(true|false)|/images/sha256%3A[0-9a-f]+/json")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("docker", timeout=TIMEOUT)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerClient:
    """Accepts unix:///path/to.sock, or tcp://host:port for socket proxies."""

    def __init__(self, host: str) -> None:
        u = urlsplit(host)
        self._connect: Callable[[], http.client.HTTPConnection]
        if u.scheme == "unix":
            self._connect = lambda: _UnixConnection(u.path)
        elif u.scheme in ("tcp", "http"):
            self._connect = lambda: http.client.HTTPConnection(u.netloc, timeout=TIMEOUT)
        elif u.scheme == "https":
            self._connect = lambda: http.client.HTTPSConnection(u.netloc, timeout=TIMEOUT)
        else:
            raise ValueError(f"unsupported DOCKER_HOST scheme {u.scheme!r}")

    def _get(self, path: str) -> Any:
        if not _ALLOWED.fullmatch(path):
            raise ValueError(f"docker {path}: not an allowed read-only call")
        conn = self._connect()
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise OSError(f"docker {path}: response over {MAX_BODY >> 20} MB")
            if resp.status != 200:
                raise OSError(f"docker {path}: {resp.status} {resp.reason}")
            return json.loads(body)
        finally:
            conn.close()

    def hostname(self) -> str:
        """The engine host's name, which the container can't see itself ($HOSTNAME is the container ID)."""
        return str(self._get("/info").get("Name") or "")

    def containers(self, include_stopped: bool = False) -> list[Container]:
        out = []
        for c in self._get(f"/containers/json?all={str(include_stopped).lower()}"):
            name = (c.get("Names") or [""])[0].lstrip("/")
            image = c.get("Image", "")
            # The listing shows a bare digest when the tag has since moved to a newer image.
            if image.startswith("sha256:"):
                try:
                    tags = self._get("/images/" + quote(c["ImageID"], safe="") + "/json").get("RepoTags") or []
                    image = tags[0] if tags else image
                except (OSError, ValueError):
                    pass
            out.append(Container(name, image, c.get("Labels") or {}))
        return out
