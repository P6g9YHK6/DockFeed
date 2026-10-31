from datetime import UTC, datetime

from dockfeed.feed import FeedEntry, Item, parse_feed, render_atom
from dockfeed.source import Repo

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.0.0</id>
    <updated>2026-01-01T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/a/b/releases/tag/v1.0.0"/>
    <title>v1.0.0</title>
    <content type="html">&lt;p&gt;old&lt;/p&gt;</content>
    <author><name>alice</name></author>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.1.0</id>
    <updated>2026-02-01T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/a/b/releases/tag/v1.1.0"/>
    <title>v1.1.0</title>
    <content type="html">&lt;p&gt;new&lt;/p&gt;</content>
  </entry>
</feed>"""

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>v16.0.7</title>
      <link>https://codeberg.org/forgejo/forgejo/releases/tag/v16.0.7</link>
      <description>&lt;p&gt;notes&lt;/p&gt;</description>
      <dc:creator>forgejo-release-manager</dc:creator>
      <pubDate>Tue, 20 Oct 2026 08:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


def test_parse_atom() -> None:
    items = parse_feed(ATOM)
    assert [i.title for i in items] == ["v1.1.0", "v1.0.0"]
    assert items[0].content == "<p>new</p>"
    assert items[0].link.endswith("v1.1.0")
    assert items[1].author == "alice"


def test_parse_rss() -> None:
    (item,) = parse_feed(RSS)
    assert item.id == item.link
    assert item.author == "forgejo-release-manager"
    assert item.updated == datetime(2026, 10, 20, 8, tzinfo=UTC)


def test_render_round_trip() -> None:
    entry = FeedEntry(
        Item(
            "x1",
            "fix things\n\nlong body",
            "https://example.com/c/1",
            datetime(2026, 3, 1, tzinfo=UTC),
            content="<p>hi &amp; bye</p>",
        ),
        label="app",
        kind="commit",
        repo=Repo("github.com", "a/b"),
        containers=["app"],
        running="v1",
    )
    out = render_atom("Test feed", "http://localhost/feed.atom", [entry])
    (item,) = parse_feed(out)
    assert item.title == "app (commit): fix things"
    assert "hi &amp; bye" in item.content
    assert "running v1" in item.content


def test_render_empty_feed_is_valid() -> None:
    assert parse_feed(render_atom("Empty", "http://x/", [])) == []
