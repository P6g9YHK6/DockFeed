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


@pytest.mark.parametrize(
    "path",
    ["/containers/create", "/containers/web/exec", "/images/sha256%3A2%2F..%2Fx/json", "/info/../containers/json"],
)
def test_refuses_anything_but_the_read_only_calls(sock: str, path: str) -> None:
    with pytest.raises(ValueError):
        DockerClient("unix://" + sock)._get(path)


@pytest.fixture
def tcp() -> Iterator[str]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"tcp://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_same_calls_and_allowlist_over_tcp(tcp: str) -> None:
    client = DockerClient(tcp)
    assert [c.name for c in client.containers()] == ["web", "moved"]
    assert client.hostname() == "Tower"
    with pytest.raises(ValueError):
        client._get("/containers/web/exec")
