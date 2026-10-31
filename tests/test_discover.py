import pytest

from dockfeed.discover import Candidate, Discoverer, name_match, owner_match, rank
from dockfeed.forge import Forge, GitHubRepoInfo, NotFoundError
from dockfeed.source import Repo


@pytest.mark.parametrize(
    ("repo", "image"),
    [
        ("portainer", "portainer-ee"),
        ("docker-autoheal", "autoheal"),
        ("umap", "umap"),
        ("duplicacy-web", "duplicacy-web-docker"),
    ],
)
def test_name_match(repo: str, image: str) -> None:
    assert name_match(repo, image)


@pytest.mark.parametrize(
    ("repo", "image"), [("online", "code"), ("portainer-docs", "portainer-ee"), ("umap", "umapper")]
)
def test_name_mismatch(repo: str, image: str) -> None:
    assert not name_match(repo, image)


@pytest.mark.parametrize(
    ("owner", "ns"), [("umap-project", "umap"), ("CollaboraOnline", "collabora"), ("Portainer", "portainer")]
)
def test_owner_match(owner: str, ns: str) -> None:
    assert owner_match(owner, ns)


@pytest.mark.parametrize(("owner", "ns"), [("lmcinnes", "umap"), ("dirtsimple", "analogic"), ("golang", "go")])
def test_owner_mismatch(owner: str, ns: str) -> None:
    assert not owner_match(owner, ns)


def test_rank() -> None:
    ranked = rank(
        [
            Candidate(Repo("github.com", "CollaboraOnline/online.mirror"), owner=True, stars=10, mentions=2),
            Candidate(Repo("github.com", "CollaboraOnline/online"), owner=True, stars=2000, mentions=1),
            Candidate(Repo("github.com", "other/code"), name=True, archived=True),
        ]
    )
    assert [c.repo.path for c in ranked] == ["other/code", "CollaboraOnline/online", "CollaboraOnline/online.mirror"]


class FakeFetcher:
    def __init__(
        self, existing: set[str], hub: dict[str, object] | None = None, search: list[object] | None = None
    ) -> None:
        self.existing = existing
        self.hub = hub
        self.search = search or []
        self.searches = 0

    def exists(self, repo: Repo) -> bool:
        return str(repo) in self.existing

    def kind(self, repo: Repo) -> Forge:
        return Forge.GITLAB if repo.host == "mau.dev" else Forge.UNKNOWN

    def get_json(self, url: str) -> object:
        if self.hub is None:
            raise NotFoundError(url)
        return self.hub

    def github_repo(self, path: str) -> GitHubRepoInfo:
        if f"github.com/{path}" not in self.existing:
            raise NotFoundError(path)
        return GitHubRepoInfo(path, False, 100 if path.endswith("online") else 1)

    def github_search(self, q: str) -> list[object]:
        self.searches += 1
        if self.search == ["rate limited"]:
            raise OSError("403")
        return self.search


def disc(f: FakeFetcher) -> Discoverer:
    return Discoverer(f)  # type: ignore[arg-type]


def test_registry_sibling() -> None:
    d = disc(FakeFetcher({"mau.dev/mautrix/signal"}))
    assert d.discover("dock.mau.dev/mautrix/signal:latest") == (
        Repo("mau.dev", "mautrix/signal"),
        "forge next to the registry",
    )


def test_hub_guess() -> None:
    d = disc(FakeFetcher({"github.com/willfarrell/docker-autoheal"}))
    assert d.discover("willfarrell/autoheal")[0] == Repo("github.com", "willfarrell/docker-autoheal")  # type: ignore[index]


def test_hub_links_skip_noise_and_rank_by_stars() -> None:
    readme = (
        "built on https://github.com/docker-library/official-images and "
        "https://github.com/CollaboraOnline/online.mirror https://github.com/CollaboraOnline/online.mirror "
        "https://github.com/CollaboraOnline/online"
    )
    f = FakeFetcher(
        {"github.com/CollaboraOnline/online", "github.com/CollaboraOnline/online.mirror"},
        hub={"full_description": readme},
    )
    assert disc(f).discover("collabora/code") == (
        Repo("github.com", "CollaboraOnline/online"),
        "Docker Hub description",
    )


def test_github_search_is_strict() -> None:
    hits = [GitHubRepoInfo("lmcinnes/umap", False, 8000), GitHubRepoInfo("umap-project/umap", False, 1600)]
    assert disc(FakeFetcher(set(), search=hits)).discover("umap/umap") == (
        Repo("github.com", "umap-project/umap"),
        "GitHub search",
    )
    assert disc(FakeFetcher(set(), search=hits[:1])).discover("umap/umap") is None


def test_misses_are_cached_but_errors_are_not() -> None:
    f = FakeFetcher(set())
    d = disc(f)
    assert d.discover("analogic/poste.io") is None
    assert d.discover("analogic/poste.io") is None
    assert f.searches == 1
    g = FakeFetcher(set(), search=["rate limited"])
    e = disc(g)
    e.discover("analogic/poste.io")
    e.discover("analogic/poste.io")
    assert g.searches == 2
