package main

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"io"
	"net/url"
	"sort"
	"strings"
	"time"
)

// Item is a single feed entry, normalized from either Atom or RSS.
type Item struct {
	ID      string
	Title   string
	Link    string
	Updated time.Time
	Author  string
	Content string
}

type atomLink struct {
	Href string `xml:"href,attr"`
	Rel  string `xml:"rel,attr"`
}

type atomText struct {
	Type string `xml:"type,attr"`
	Body string `xml:",chardata"`
}

type rawFeed struct {
	// Atom
	Entries []struct {
		ID        string     `xml:"id"`
		Title     string     `xml:"title"`
		Links     []atomLink `xml:"link"`
		Updated   string     `xml:"updated"`
		Published string     `xml:"published"`
		Author    string     `xml:"author>name"`
		Content   atomText   `xml:"content"`
		Summary   atomText   `xml:"summary"`
	} `xml:"entry"`
	// RSS 2.0
	Items []struct {
		GUID        string `xml:"guid"`
		Title       string `xml:"title"`
		Link        string `xml:"link"`
		PubDate     string `xml:"pubDate"`
		Author      string `xml:"author"`
		Creator     string `xml:"http://purl.org/dc/elements/1.1/ creator"`
		Description string `xml:"description"`
		Content     string `xml:"http://purl.org/rss/1.0/modules/content/ encoded"`
	} `xml:"channel>item"`
}

var dateLayouts = []string{
	time.RFC3339, time.RFC1123Z, time.RFC1123, time.RFC822Z, time.RFC822,
	"Mon, 2 Jan 2006 15:04:05 -0700", "Mon, 2 Jan 2006 15:04:05 MST",
}

func parseDate(s string) time.Time {
	s = strings.TrimSpace(s)
	for _, l := range dateLayouts {
		if t, err := time.Parse(l, s); err == nil {
			return t
		}
	}
	return time.Time{}
}

// parseFeed reads Atom or RSS and returns entries newest first.
func parseFeed(r io.Reader) ([]Item, error) {
	var f rawFeed
	dec := xml.NewDecoder(r)
	dec.Strict = false
	dec.CharsetReader = func(_ string, in io.Reader) (io.Reader, error) { return in, nil }
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("parse feed: %w", err)
	}
	var items []Item
	for _, e := range f.Entries {
		it := Item{ID: e.ID, Title: strings.TrimSpace(e.Title), Author: e.Author}
		for _, l := range e.Links {
			if l.Rel == "" || l.Rel == "alternate" {
				it.Link = l.Href
				break
			}
		}
		it.Updated = parseDate(e.Updated)
		if it.Updated.IsZero() {
			it.Updated = parseDate(e.Published)
		}
		it.Content = textAsHTML(e.Content)
		if it.Content == "" {
			it.Content = textAsHTML(e.Summary)
		}
		items = append(items, it)
	}
	for _, e := range f.Items {
		it := Item{ID: e.GUID, Title: strings.TrimSpace(e.Title), Link: strings.TrimSpace(e.Link), Updated: parseDate(e.PubDate)}
		it.Author = e.Creator
		if it.Author == "" {
			it.Author = e.Author
		}
		it.Content = e.Content
		if it.Content == "" {
			it.Content = e.Description
		}
		items = append(items, it)
	}
	for i := range items {
		if items[i].ID == "" {
			items[i].ID = items[i].Link
		}
	}
	sortItems(items)
	return items, nil
}

func textAsHTML(t atomText) string {
	if t.Type == "text" || t.Type == "" && !strings.Contains(t.Body, "<") {
		return xmlEscape(strings.TrimSpace(t.Body))
	}
	return strings.TrimSpace(t.Body)
}

func xmlEscape(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(s))
	return b.String()
}

func sortItems(items []Item) {
	// Stable so items with equal (or missing) dates keep the source order.
	sort.SliceStable(items, func(i, j int) bool { return items[i].Updated.After(items[j].Updated) })
}

type outLink struct {
	Href string `xml:"href,attr"`
	Rel  string `xml:"rel,attr,omitempty"`
	Type string `xml:"type,attr,omitempty"`
}

type outText struct {
	Type string `xml:"type,attr,omitempty"`
	Body string `xml:",chardata"`
}

type outEntry struct {
	ID       string   `xml:"id"`
	Title    string   `xml:"title"`
	Link     outLink  `xml:"link"`
	Updated  string   `xml:"updated"`
	Author   *outName `xml:"author,omitempty"`
	Category []outCat `xml:"category"`
	Content  outText  `xml:"content"`
}

type outName struct {
	Name string `xml:"name"`
}

type outCat struct {
	Term string `xml:"term,attr"`
}

type outFeed struct {
	XMLName xml.Name   `xml:"http://www.w3.org/2005/Atom feed"`
	ID      string     `xml:"id"`
	Title   string     `xml:"title"`
	Updated string     `xml:"updated"`
	Links   []outLink  `xml:"link"`
	Entries []outEntry `xml:"entry"`
}

// FeedEntry is an Item attributed to the containers it concerns.
type FeedEntry struct {
	Item
	Label      string
	Kind       string // "release" or "commit"
	Containers []string
	Running    string
	Repo       Repo
}

func renderAtom(w io.Writer, title, selfURL string, entries []FeedEntry) error {
	f := outFeed{ID: "urn:dockfeed:" + url.PathEscape(title), Title: title, Links: []outLink{{Href: selfURL, Rel: "self", Type: "application/atom+xml"}}}
	var updated time.Time
	for _, e := range entries {
		if e.Updated.After(updated) {
			updated = e.Updated
		}
		t := e.Label + ": " + e.Title
		if e.Kind == "commit" {
			// Commit titles can be whole messages; the first line is enough for a headline.
			first, _, _ := strings.Cut(e.Title, "\n")
			t = e.Label + " (commit): " + strings.TrimSpace(first)
		}
		footer := fmt.Sprintf(`<hr/><p><a href="%s">%s</a> &middot; used by %s`,
			xmlEscape(e.Repo.URL()), xmlEscape(e.Repo.String()), xmlEscape(strings.Join(e.Containers, ", ")))
		if e.Running != "" {
			footer += " &middot; running " + xmlEscape(e.Running)
		}
		footer += "</p>"
		oe := outEntry{
			ID:      e.ID,
			Title:   t,
			Link:    outLink{Href: e.Link, Rel: "alternate"},
			Updated: e.Updated.UTC().Format(time.RFC3339),
			Content: outText{Type: "html", Body: e.Content + footer},
		}
		if e.Author != "" {
			oe.Author = &outName{Name: e.Author}
		}
		for _, c := range e.Containers {
			oe.Category = append(oe.Category, outCat{Term: c})
		}
		oe.Category = append(oe.Category, outCat{Term: e.Kind})
		f.Entries = append(f.Entries, oe)
	}
	if updated.IsZero() {
		updated = time.Now()
	}
	f.Updated = updated.UTC().Format(time.RFC3339)
	if _, err := io.WriteString(w, xml.Header); err != nil {
		return err
	}
	enc := xml.NewEncoder(w)
	enc.Indent("", "  ")
	return enc.Encode(f)
}
