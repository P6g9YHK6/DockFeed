package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"sync"
	"time"
)

type forgeKind int

const (
	forgeUnknown forgeKind = iota
	forgeGitHub
	forgeGitea // also Forgejo and Codeberg
	forgeGitLab
)

var errNotFound = errors.New("not found")

type cached struct {
	etag, lastModified string
	items              []Item
}

// fetcher talks to forges and remembers validators so unchanged feeds cost a 304.
type fetcher struct {
	http      *http.Client
	userAgent string
	ghToken   string

	mu       sync.Mutex
	feeds    map[string]cached
	kinds    map[string]forgeKind
	branches map[string]string
	known    map[string]bool
}

func newFetcher(userAgent, ghToken string) *fetcher {
	return &fetcher{
		ghToken:   ghToken,
		http:      &http.Client{Timeout: 30 * time.Second},
		userAgent: userAgent,
		feeds:     map[string]cached{},
		kinds:     map[string]forgeKind{},
		branches:  map[string]string{},
		known:     map[string]bool{},
	}
}

func (f *fetcher) do(ctx context.Context, u string, hdr map[string]string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", f.userAgent)
	if f.ghToken != "" && req.URL.Host == "api.github.com" {
		req.Header.Set("Authorization", "Bearer "+f.ghToken)
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	return f.http.Do(req)
}

func (f *fetcher) feed(ctx context.Context, u string) ([]Item, error) {
	f.mu.Lock()
	prev, hasPrev := f.feeds[u]
	f.mu.Unlock()
	hdr := map[string]string{"Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.1"}
	if hasPrev {
		if prev.etag != "" {
			hdr["If-None-Match"] = prev.etag
		}
		if prev.lastModified != "" {
			hdr["If-Modified-Since"] = prev.lastModified
		}
	}
	resp, err := f.do(ctx, u, hdr)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotModified && hasPrev:
		return prev.items, nil
	case resp.StatusCode == http.StatusNotFound:
		return nil, fmt.Errorf("%s: %w", u, errNotFound)
	case resp.StatusCode != http.StatusOK:
		return nil, fmt.Errorf("%s: %s", u, resp.Status)
	}
	items, err := parseFeed(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", u, err)
	}
	f.mu.Lock()
	f.feeds[u] = cached{etag: resp.Header.Get("ETag"), lastModified: resp.Header.Get("Last-Modified"), items: items}
	f.mu.Unlock()
	return items, nil
}

func (f *fetcher) getJSON(ctx context.Context, u string, out any) error {
	resp, err := f.do(ctx, u, map[string]string{"Accept": "application/json"})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return errNotFound
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: %s", u, resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// kind identifies the forge software behind a host, probing self-hosted instances once.
func (f *fetcher) kind(ctx context.Context, r Repo) forgeKind {
	switch r.Host {
	case "github.com":
		return forgeGitHub
	case "gitlab.com":
		return forgeGitLab
	case "codeberg.org", "code.forgejo.org", "gitea.com":
		return forgeGitea
	}
	f.mu.Lock()
	k, ok := f.kinds[r.Host]
	f.mu.Unlock()
	if ok {
		return k
	}
	var v struct{ Version string }
	var p struct{ ID int }
	switch {
	case f.getJSON(ctx, "https://"+r.Host+"/api/v1/version", &v) == nil && v.Version != "":
		k = forgeGitea
	case f.getJSON(ctx, "https://"+r.Host+"/api/v4/projects/"+url.PathEscape(r.Path), &p) == nil && p.ID != 0:
		k = forgeGitLab
	}
	f.mu.Lock()
	f.kinds[r.Host] = k
	f.mu.Unlock()
	return k
}

func (f *fetcher) defaultBranch(ctx context.Context, r Repo, k forgeKind) (string, error) {
	f.mu.Lock()
	b, ok := f.branches[r.String()]
	f.mu.Unlock()
	if ok {
		return b, nil
	}
	var info struct {
		DefaultBranch string `json:"default_branch"`
	}
	var u string
	switch k {
	case forgeGitea:
		u = "https://" + r.Host + "/api/v1/repos/" + r.Path
	case forgeGitLab:
		u = "https://" + r.Host + "/api/v4/projects/" + url.PathEscape(r.Path)
	default:
		return "", fmt.Errorf("no branch lookup for %s", r)
	}
	if err := f.getJSON(ctx, u, &info); err != nil {
		return "", err
	}
	if info.DefaultBranch == "" {
		return "", fmt.Errorf("%s: empty default branch", r)
	}
	f.mu.Lock()
	f.branches[r.String()] = info.DefaultBranch
	f.mu.Unlock()
	return info.DefaultBranch, nil
}

// releases returns the repo's releases. GitHub and GitLab list tags here when there are no releases.
func (f *fetcher) releases(ctx context.Context, r Repo) ([]Item, error) {
	base := r.URL()
	switch f.kind(ctx, r) {
	case forgeGitHub:
		return f.feed(ctx, base+"/releases.atom")
	case forgeGitea:
		return f.feed(ctx, base+"/releases.rss")
	case forgeGitLab:
		return f.feed(ctx, base+"/-/tags?format=atom")
	}
	return nil, fmt.Errorf("%s: unrecognized forge", r.Host)
}

// commits returns recent commits on branch, or on the default branch when branch is empty.
func (f *fetcher) commits(ctx context.Context, r Repo, branch string) ([]Item, error) {
	base := r.URL()
	k := f.kind(ctx, r)
	if branch == "" && k != forgeGitHub {
		var err error
		if branch, err = f.defaultBranch(ctx, r, k); err != nil {
			return nil, err
		}
	}
	switch k {
	case forgeGitHub:
		if branch == "" {
			return f.feed(ctx, base+"/commits.atom")
		}
		return f.feed(ctx, base+"/commits/"+url.PathEscape(branch)+".atom")
	case forgeGitea:
		return f.feed(ctx, base+"/rss/branch/"+url.PathEscape(branch))
	case forgeGitLab:
		return f.feed(ctx, base+"/-/commits/"+url.PathEscape(branch)+"?format=atom")
	}
	return nil, fmt.Errorf("%s: unrecognized forge", r.Host)
}

// exists reports whether a guessed repo is real, remembering the answer for the process lifetime.
func (f *fetcher) exists(ctx context.Context, r Repo) bool {
	f.mu.Lock()
	ok, seen := f.known[r.String()]
	f.mu.Unlock()
	if seen {
		return ok
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, r.URL(), nil)
	if err != nil {
		return false
	}
	req.Header.Set("User-Agent", f.userAgent)
	resp, err := f.http.Do(req)
	if err != nil {
		// Network trouble is not an answer; try again next scan.
		return false
	}
	resp.Body.Close()
	ok = resp.StatusCode == http.StatusOK
	if ok || resp.StatusCode == http.StatusNotFound {
		f.mu.Lock()
		f.known[r.String()] = ok
		f.mu.Unlock()
	}
	return ok
}
