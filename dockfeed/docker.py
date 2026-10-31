"""A minimal Docker Engine API client: two read-only calls over the socket or TCP."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

from .source import Container

TIMEOUT = 30


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
        conn = self._connect()
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
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
                    tags = self._get("/images/" + quote(c["ImageID"]) + "/json").get("RepoTags") or []
                    image = tags[0] if tags else image
                except OSError:
                    pass
            out.append(Container(name, image, c.get("Labels") or {}))
        return out
