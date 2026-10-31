package main

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

const atomSample = `<?xml version="1.0" encoding="UTF-8"?>
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
</feed>`

const rssSample = `<?xml version="1.0" encoding="UTF-8"?>
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
</rss>`

func TestParseAtom(t *testing.T) {
	items, err := parseFeed(strings.NewReader(atomSample))
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 2 || items[0].Title != "v1.1.0" || items[1].Author != "alice" {
		t.Fatalf("unexpected items: %+v", items)
	}
	if items[0].Content != "<p>new</p>" || !strings.HasSuffix(items[0].Link, "v1.1.0") {
		t.Errorf("content/link: %+v", items[0])
	}
}

func TestParseRSS(t *testing.T) {
	items, err := parseFeed(strings.NewReader(rssSample))
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 {
		t.Fatalf("got %d items", len(items))
	}
	it := items[0]
	if it.ID != it.Link || it.Author != "forgejo-release-manager" || it.Updated.IsZero() {
		t.Errorf("unexpected item: %+v", it)
	}
}

func TestRenderAtomRoundTrip(t *testing.T) {
	entries := []FeedEntry{{
		Item:       Item{ID: "x1", Title: "fix things\n\nlong body", Link: "https://example.com/c/1", Updated: time.Date(2026, 3, 1, 0, 0, 0, 0, time.UTC), Content: "<p>hi & bye</p>"},
		Label:      "app",
		Kind:       "commit",
		Containers: []string{"app"},
		Repo:       Repo{Host: "github.com", Path: "a/b"},
	}}
	var buf bytes.Buffer
	if err := renderAtom(&buf, "Test feed", "http://localhost/feed.atom", entries); err != nil {
		t.Fatal(err)
	}
	items, err := parseFeed(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].Title != "app (commit): fix things" || !strings.Contains(items[0].Content, "hi &amp; bye") && !strings.Contains(items[0].Content, "hi & bye") {
		t.Errorf("round trip: %+v", items)
	}
}
