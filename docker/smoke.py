"""Runs inside the finished scratch filesystem at build time.

Exercises every stdlib area DockFeed touches so a module pruned too eagerly by
slim.sh fails the build, not the container.
"""

import ssl
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer  # noqa: F401
from urllib.request import Request

from dockfeed import app, discover, docker, feed, forge, source

atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id>
<title>v1</title><updated>2026-01-01T00:00:00Z</updated><link href="https://x/1"/>
<content type="html">&lt;p&gt;hi&lt;/p&gt;</content></entry></feed>"""
rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>v2</title>
<link>https://x/2</link><pubDate>Tue, 20 Oct 2026 08:00:00 +0000</pubDate></item></channel></rss>"""

items = feed.parse_feed(atom) + feed.parse_feed(rss)
assert [i.title for i in items] == ["v1", "v2"], items
entry = feed.FeedEntry(items[0], "app", "release", source.Repo("github.com", "a/b"), ["app"])
assert feed.parse_feed(feed.render_atom("t", "http://x/", [entry]))[0].title == "app: v1"

ctx = ssl.create_default_context()
assert ctx.cert_store_stats()["x509_ca"] > 100, "CA bundle missing"

with ThreadPoolExecutor(2) as pool:
    assert list(pool.map(source.parse_repo_url, ["github.com/a/b"])) == [source.Repo("github.com", "a/b")]

app.Config.from_env({"EXCLUDE": "(?i)rc", "REFRESH_INTERVAL": "1h"})
forge.Fetcher("smoke")._request("https://api.github.com/x")
Request("https://example.com", method="HEAD")
docker.DockerClient("unix:///var/run/docker.sock")
discover.Discoverer(forge.Fetcher("smoke"))

open("/smoke-ok", "w").close()
print("smoke ok")
