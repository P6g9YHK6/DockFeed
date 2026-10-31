<p align="center"><img src="assets/icon.svg" width="128" alt="DockFeed icon"></p>

# DockFeed

[![Lint and test](https://github.com/P6g9YHK6/DockFeed/actions/workflows/test.yml/badge.svg)](https://github.com/P6g9YHK6/DockFeed/actions/workflows/test.yml)
[![Publish Docker image](https://github.com/P6g9YHK6/DockFeed/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/P6g9YHK6/DockFeed/actions/workflows/docker-publish.yml)
[![License: MPL 2.0](https://img.shields.io/badge/license-MPL%202.0-blue.svg)](LICENSE)

One Atom feed with the release notes of everything running on your Docker host.

DockFeed reads the Docker socket, works out which source repository each container comes from, and merges their release feeds into a single feed you can subscribe to in any reader. When a project doesn't publish releases (or stopped tagging long ago), it follows the commits on the default branch instead.

No database, no required API tokens, no config file. Plain Python with a single dependency (feedparser), shipped as a 24 MB `scratch` image (about 9 MB to download) for amd64, arm64 and armv7.

## Quick start

```yaml
services:
  dockfeed:
    image: ghcr.io/p6g9yhk6/dockfeed:latest
    container_name: dockfeed
    restart: unless-stopped
    ports:
      - 8080:8080
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

Then subscribe to `http://<host>:8080/feed.atom`. The first scan takes a few seconds; `/status` shows what was found.

### Image tags

| Tag | Built from |
| --- | --- |
| `latest` | Every push to `main` |
| `1.2.3`, `1.2` | Release tags `v1.2.3` |
| `sha-abc1234` | A specific commit |

## Endpoints

| Path | What |
| --- | --- |
| `/feed.atom` (or `/`) | The merged feed, newest first |
| `/feed.atom?container=sonarr,radarr` | Only these containers |
| `/feed.atom?kind=release` | Drop the commit fallbacks, `kind=commit` for the opposite |
| `/status` | JSON: every repo, how it was resolved, errors, and which containers couldn't be resolved |
| `/healthz` | Liveness |

Each entry is titled `<container>: <release>` (or `<container> (commit): <message>`), keeps the original release notes, and ends with a line linking the repo, the containers using it, and the version currently running when the image says so. Containers are also set as Atom categories, so readers that filter on tags can use them.

## How a container is matched to a repo

First match wins:

1. `dockfeed.source` label on the container
2. `org.opencontainers.image.source` or `org.label-schema.vcs-url` image label (most images built with GitHub Actions have one)
3. `org.opencontainers.image.url` when it points at a known forge
4. The image name: `ghcr.io/owner/repo`, `codeberg.org/...`, `registry.gitlab.com/...`, linuxserver.io images (`lscr.io/linuxserver/x` maps to `linuxserver/docker-x`), and a built-in list of popular Docker Hub official images (`redis`, `postgres`, `nginx`, ...)

If none of that works, DockFeed looks the image up (disable with `DISCOVER=false`):

5. A forge next to the registry: `dock.mau.dev/mautrix/signal` is found at `mau.dev/mautrix/signal` (GitLab, Gitea and Forgejo are detected through their API)
6. For Docker Hub `owner/name` images, `github.com/owner/name` then `github.com/owner/docker-name`
7. Repo links in the image's Docker Hub description, preferring a link whose name matches the image, then one whose owner matches the namespace, then the most starred. Packaging repos like `docker-library/*` are ignored
8. A GitHub search, keeping only a repo whose owner matches the Docker Hub namespace and whose name matches the image (`umap/umap` finds `umap-project/umap`, `portainer/portainer-ee` finds `portainer/portainer`, never an unrelated repo that happens to share the name)

Every candidate is checked to exist before it's used. Results are cached: a match for the life of the process, a miss for 24 hours. Rate limits or network errors aren't cached, so the lookup is retried on the next scan. `/status` shows which step matched each repo.

Containers sharing a repo (an app plus its worker, for example) are merged into one set of entries. Anything left over, typically closed-source images, is listed under `unresolved` in `/status`. Add a `dockfeed.source` label to point it somewhere, or `dockfeed.enable=false` to silence it.

## Releases or commits

In the default `auto` mode, DockFeed uses the repo's releases, and switches to commits when:

- the repo has no releases and no tags, or
- the newest release is older than `STALE_RELEASE_AFTER` (one year by default)

GitHub and GitLab list tags in place of releases when a repo only tags, so tag-only projects are still treated as releasing.

Supported forges: GitHub, GitLab (gitlab.com and self-hosted), Gitea, Forgejo and Codeberg. Self-hosted instances are detected through their API.

## Container labels

| Label | Effect |
| --- | --- |
| `dockfeed.enable=false` | Leave this container out |
| `dockfeed.source=https://github.com/owner/repo` | Override the detected repo |
| `dockfeed.mode=releases` | `auto` (default), `releases` or `commits` |
| `dockfeed.branch=dev` | Branch to follow in commit mode, defaults to the repo's default branch. Handy for images that track `main` or `dev` |
| `dockfeed.include=^v\d` | Only keep entries whose title matches this regex |
| `dockfeed.exclude=(?i)rc\|beta` | Drop entries whose title matches, overrides the global `EXCLUDE` |

Filters run before the staleness check, so a project that only ships filtered-out prereleases falls back to commits.

## Environment

| Variable | Default | |
| --- | --- | --- |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | `tcp://socket-proxy:2375` works too |
| `LISTEN_ADDR` | `:8080` | |
| `REFRESH_INTERVAL` | `1h` | `90s`, `30m`, `1h30m`, `1d`... |
| `STALE_RELEASE_AFTER` | `365d` | `0` never falls back on age |
| `ITEMS_PER_REPO` | `10` | |
| `MAX_ITEMS` | `500` | Cap on the merged feed, applied after `?container=` filtering |
| `CONCURRENCY` | `8` | Parallel forge requests |
| `EXCLUDE` | | Regex on titles applied to every repo, e.g. `(?i)-rc\|beta\|alpha\|nightly` |
| `INCLUDE_STOPPED` | `false` | Also follow stopped containers |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every HTTP request |
| `DISCOVER` | `true` | Steps 5 to 8 above |
| `GITHUB_TOKEN` | | Optional, any token with no scopes. Raises the GitHub API limit used by steps 7 and 8 from 60 to 5000 requests an hour |
| `FEED_TITLE` | `DockFeed` | |
| `PUBLIC_URL` | | Base URL for the feed's self link when behind a proxy |

## Security

The socket is only used for two read-only calls (list containers, inspect an image). Mount it `:ro`, or better, point `DOCKER_HOST` at a socket proxy that only allows `CONTAINERS` and `IMAGES`:

```yaml
  socket-proxy:
    image: lscr.io/linuxserver/socket-proxy:latest
    environment:
      CONTAINERS: 1
      IMAGES: 1
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
  dockfeed:
    image: ghcr.io/p6g9yhk6/dockfeed:latest
    environment:
      DOCKER_HOST: tcp://socket-proxy:2375
```

The feed lists everything you run and which versions, so don't expose it publicly without auth.

Release and commit feeds are fetched without credentials, with conditional requests so an unchanged feed costs a 304.

## Development

Needs Python 3.12 or newer.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m dockfeed --once > feed.atom   # one scan, print the feed, exit
python -m dockfeed                      # serve on :8080
pytest --cov                            # tests, fails under 80% coverage
ruff check . && ruff format --check . && mypy dockfeed
```

`DOCKER_HOST` can point at a remote engine for testing, for example through an SSH-forwarded socket:

```sh
ssh -NL /tmp/remote.sock:/var/run/docker.sock myserver &
DOCKER_HOST=unix:///tmp/remote.sock python -m dockfeed --once
```

The code is split by concern, all under `dockfeed/`: `docker.py` (socket client), `source.py` (label and image name rules), `discover.py` (network lookups for unlabeled images), `forge.py` (GitHub, GitLab, Gitea and Forgejo feeds), `feed.py` (Atom and RSS in, Atom out) and `app.py` (config, scan loop, HTTP). Apart from feedparser everything is the standard library.

The image is built from `python:3.14-alpine`, then `docker/slim.sh` copies only what DockFeed needs onto `scratch`: the interpreter, the stdlib minus tests, GUI, REPL, database and other unused modules (as bytecode only), the shared libraries those link against, and the CA bundle. A smoke stage (`docker/smoke.py`) runs inside that filesystem during the build, so pruning too much fails the build rather than the container. If a new import fails in the image only, the module was pruned: take it out of the `rm` lists in `slim.sh`. Tracebacks in the image show no source lines, since only bytecode ships.

`requirements.lock` is the hash-pinned resolution of `requirements.txt` that the image installs. After changing `requirements.txt`, regenerate it with:

```sh
uv pip compile requirements.txt -o requirements.lock --python-version 3.14 --generate-hashes
```

### CI

- **Lint and test** runs on every push and pull request: ruff (lint and format), mypy in strict mode, pip-audit on the locked dependencies, hadolint on the Dockerfile, pytest on Python 3.12 and 3.14, and a check that `requirements.lock` still matches `requirements.txt`. `.pre-commit-config.yaml` runs the same ruff checks locally (`pip install pre-commit && pre-commit install`).
- **Publish Docker image** builds the multi-arch image and pushes it to GHCR on every push to `main` and on `v*.*.*` tags.
- Dependabot opens weekly grouped PRs for the Python dependencies, the base image and the actions. They're merged automatically once lint and tests pass; for Python bumps the lock file is regenerated first.

To cut a release: `git tag v1.0.0 && git push --tags`.

### Contributing

Issues and pull requests are welcome. If an image isn't matched to its repo, open an issue with the image name and the repo it should map to. Most of the time the right fix is a rule in `dockfeed/source.py` or an entry in the official image list, which helps everyone running it.

## License

[Mozilla Public License 2.0](LICENSE)
