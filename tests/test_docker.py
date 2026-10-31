import json
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from dockfeed.docker import DockerClient

CONTAINERS = [
    {"Names": ["/web"], "Image": "nginx:alpine", "ImageID": "sha256:1", "Labels": {"a": "b"}},
    {"Names": ["/moved"], "Image": "sha256:2", "ImageID": "sha256:2", "Labels": None},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/containers/json"):
            body = json.dumps(CONTAINERS).encode()
        elif self.path == "/info":
            body = json.dumps({"Name": "Tower"}).encode()
        elif self.path == "/images/sha256%3A2/json" or self.path == "/images/sha256:2/json":
            body = json.dumps({"RepoTags": ["redis:alpine"]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self) -> str:
        return "unix"

    def log_message(self, *args: object) -> None:
        pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def sock(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "docker.sock")
    server = UnixServer(path, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield path
    server.shutdown()


def test_containers_over_unix_socket(sock: str) -> None:
    cs = DockerClient("unix://" + sock).containers()
    assert [(c.name, c.image, c.labels) for c in cs] == [
        ("web", "nginx:alpine", {"a": "b"}),
        ("moved", "redis:alpine", {}),
    ]


def test_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError):
        DockerClient("ssh://host")
    DockerClient("tcp://socket-proxy:2375")


def test_hostname_from_info(sock: str) -> None:
    assert DockerClient("unix://" + sock).hostname() == "Tower"
