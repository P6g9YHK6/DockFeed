import pytest

from dockfeed.source import Container, guess_repos, parse_repo_url, resolve_repo, split_image


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://github.com/immich-app/immich", "github.com/immich-app/immich"),
        ("https://github.com/element-hq/synapse.git", "github.com/element-hq/synapse"),
        ("github.com/foo/bar/tree/main/docker", "github.com/foo/bar"),
        ("git@github.com:foo/bar.git", "github.com/foo/bar"),
        ("https://www.github.com/Foo/Bar/", "github.com/Foo/Bar"),
        ("https://gitlab.com/group/sub/project/-/tree/main", "gitlab.com/group/sub/project"),
        ("https://codeberg.org/forgejo/forgejo", "codeberg.org/forgejo/forgejo"),
    ],
)
def test_parse_repo_url(raw: str, want: str) -> None:
    assert str(parse_repo_url(raw)) == want


@pytest.mark.parametrize("raw", ["", None, "https://www.portainer.io", "https://github.com/onlyowner"])
def test_parse_repo_url_rejects(raw: str | None) -> None:
    assert parse_repo_url(raw) is None


@pytest.mark.parametrize(
    ("image", "want"),
    [
        ("redis", ("docker.io", "redis", "")),
        ("library/postgres:18-alpine", ("docker.io", "postgres", "18-alpine")),
        ("ghcr.io/immich-app/immich-server:v3", ("ghcr.io", "immich-app/immich-server", "v3")),
        ("localhost:5000/me/app:dev", ("localhost:5000", "me/app", "dev")),
        ("grafana/loki@sha256:abc", ("docker.io", "grafana/loki", "")),
    ],
)
def test_split_image(image: str, want: tuple[str, str, str]) -> None:
    assert split_image(image) == want


@pytest.mark.parametrize(
    ("image", "labels", "want"),
    [
        (
            "x",
            {"dockfeed.source": "https://github.com/a/b", "org.opencontainers.image.source": "https://github.com/c/d"},
            "github.com/a/b",
        ),
        ("x", {"org.opencontainers.image.source": "https://github.com/c/d"}, "github.com/c/d"),
        ("x", {"org.opencontainers.image.url": "https://www.portainer.io"}, None),
        ("x", {"org.opencontainers.image.url": "https://github.com/e/f"}, "github.com/e/f"),
        ("ghcr.io/owner/app:latest", {}, "github.com/owner/app"),
        ("lscr.io/linuxserver/sonarr", {}, "github.com/linuxserver/docker-sonarr"),
        ("data.forgejo.org/forgejo/runner:12", {}, "code.forgejo.org/forgejo/runner"),
        ("redis:alpine", {}, "github.com/redis/redis"),
        ("someone/thing", {}, None),
    ],
)
def test_resolve_repo(image: str, labels: dict[str, str], want: str | None) -> None:
    found = resolve_repo(Container("c", image, labels))
    assert (str(found[0]) if found else None) == want


def test_guess_repos() -> None:
    assert [str(r) for r in guess_repos("willfarrell/autoheal:latest")] == [
        "github.com/willfarrell/autoheal",
        "github.com/willfarrell/docker-autoheal",
    ]
    for image in ("redis", "ghcr.io/a/b", "lscr.io/linuxserver/x"):
        assert guess_repos(image) == []
