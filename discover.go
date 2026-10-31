package main

import (
	"context"
	"errors"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// discoverer finds repos for images that carry no usable labels, using network lookups.
// Every candidate is verified before use, and answers are cached so lookups don't repeat each scan.
type discoverer struct {
	f *fetcher

	mu    sync.Mutex
	cache map[string]discovery
}

type discovery struct {
	repo Repo
	via  string
	ok   bool
	at   time.Time
}

const missTTL = 24 * time.Hour

func newDiscoverer(f *fetcher) *discoverer {
	return &discoverer{f: f, cache: map[string]discovery{}}
}

func (d *discoverer) discover(ctx context.Context, image string) (Repo, string, bool) {
	registry, path, _ := splitImage(image)
	key := registry + "/" + path
	d.mu.Lock()
	c, seen := d.cache[key]
	d.mu.Unlock()
	if seen && (c.ok || time.Since(c.at) < missTTL) {
		return c.repo, c.via, c.ok
	}

	steps := []struct {
		via string
		fn  func(context.Context, string, string) (Repo, bool, error)
	}{
		{"forge next to the registry", d.registrySibling},
		{"guessed from Docker Hub name", d.hubGuess},
		{"Docker Hub description", d.hubLinks},
		{"GitHub search", d.githubSearch},
	}
	var transient bool
	for _, s := range steps {
		r, ok, err := s.fn(ctx, registry, path)
		if err != nil {
			// Rate limits and outages aren't a "no"; leave the image uncached so the next scan retries.
			transient = true
			continue
		}
		if ok {
			d.store(key, discovery{repo: r, via: s.via, ok: true})
			return r, s.via, true
		}
	}
	if !transient {
		d.store(key, discovery{at: time.Now()})
	}
	return Repo{}, "", false
}

func (d *discoverer) store(key string, c discovery) {
	d.mu.Lock()
	d.cache[key] = c
	d.mu.Unlock()
}

// registrySibling handles self-hosted registries that sit next to their forge, like dock.mau.dev and mau.dev.
func (d *discoverer) registrySibling(ctx context.Context, registry, path string) (Repo, bool, error) {
	switch registry {
	case "docker.io", "ghcr.io", "lscr.io", "quay.io", "gcr.io", "mcr.microsoft.com", "public.ecr.aws":
		return Repo{}, false, nil
	}
	hosts := []string{strings.Split(registry, ":")[0]}
	if labels := strings.Split(hosts[0], "."); len(labels) > 2 {
		hosts = append(hosts, strings.Join(labels[1:], "."))
	}
	for _, h := range hosts {
		r, ok := parseRepoURL("https://" + h + "/" + path)
		if !ok {
			continue
		}
		if d.f.kind(ctx, r) != forgeUnknown && d.f.exists(ctx, r) {
			return r, true, nil
		}
	}
	return Repo{}, false, nil
}

func (d *discoverer) hubGuess(ctx context.Context, registry, path string) (Repo, bool, error) {
	for _, g := range guessRepos(registry + "/" + path) {
		if d.f.exists(ctx, g) {
			return g, true, nil
		}
	}
	return Repo{}, false, nil
}

// Repos that show up in Docker Hub READMEs without being the image's upstream.
var hubLinkNoise = regexp.MustCompile(`(?i)^github\.com/(docker-library|docker|moby|library|opencontainers)/`)

var forgeLink = regexp.MustCompile(`https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)/([\w.-]+)/([\w.-]+)`)

func (d *discoverer) hubLinks(ctx context.Context, registry, path string) (Repo, bool, error) {
	ns, name, ok := strings.Cut(path, "/")
	if registry != "docker.io" || !ok {
		return Repo{}, false, nil
	}
	var hub struct {
		Description     string `json:"description"`
		FullDescription string `json:"full_description"`
	}
	if err := d.f.getJSON(ctx, "https://hub.docker.com/v2/repositories/"+ns+"/"+name+"/", &hub); err != nil {
		if errors.Is(err, errNotFound) {
			return Repo{}, false, nil
		}
		return Repo{}, false, err
	}
	counts := map[Repo]int{}
	for _, m := range forgeLink.FindAllStringSubmatch(hub.Description+"\n"+hub.FullDescription, -1) {
		r, ok := parseRepoURL(m[0])
		if !ok || hubLinkNoise.MatchString(r.String()) {
			continue
		}
		if nameMatch(r.Name(), name) || ownerMatch(owner(r), ns) {
			counts[r]++
		}
	}
	var cands []candidate
	for r, n := range counts {
		c := candidate{repo: r, mentions: n, name: nameMatch(r.Name(), name), owner: ownerMatch(owner(r), ns)}
		if r.Host == "github.com" {
			info, err := d.f.githubRepo(ctx, r.Path)
			if errors.Is(err, errNotFound) {
				continue
			}
			if err != nil {
				return Repo{}, false, err
			}
			c.repo, c.stars, c.archived = Repo{Host: "github.com", Path: info.FullName}, info.Stars, info.Archived
		} else if !d.f.exists(ctx, r) {
			continue
		}
		cands = append(cands, c)
	}
	if len(cands) == 0 {
		return Repo{}, false, nil
	}
	rank(cands)
	return cands[0].repo, true, nil
}

// githubSearch only accepts a hit whose owner and name both line up with the image,
// so a popular but unrelated repo with the same name is never picked.
func (d *discoverer) githubSearch(ctx context.Context, registry, path string) (Repo, bool, error) {
	ns, name, ok := strings.Cut(path, "/")
	if registry != "docker.io" || !ok {
		return Repo{}, false, nil
	}
	term := strings.FieldsFunc(stripDocker(name), func(r rune) bool { return r == '-' || r == '_' || r == '.' })
	if len(term) == 0 {
		return Repo{}, false, nil
	}
	hits, err := d.f.githubSearch(ctx, term[0]+" in:name")
	if err != nil {
		return Repo{}, false, err
	}
	var cands []candidate
	for _, h := range hits {
		r := Repo{Host: "github.com", Path: h.FullName}
		if nameMatch(r.Name(), name) && ownerMatch(owner(r), ns) {
			cands = append(cands, candidate{repo: r, name: true, owner: true, stars: h.Stars, archived: h.Archived})
		}
	}
	if len(cands) == 0 {
		return Repo{}, false, nil
	}
	rank(cands)
	return cands[0].repo, true, nil
}

type candidate struct {
	repo        Repo
	name, owner bool
	archived    bool
	stars       int
	mentions    int
}

func rank(cs []candidate) {
	score := func(c candidate) int {
		s := 0
		if c.name {
			s += 4
		}
		if c.owner {
			s += 2
		}
		if !c.archived {
			s++
		}
		return s
	}
	sort.SliceStable(cs, func(i, j int) bool {
		a, b := cs[i], cs[j]
		if score(a) != score(b) {
			return score(a) > score(b)
		}
		if a.stars != b.stars {
			return a.stars > b.stars
		}
		return a.mentions > b.mentions
	})
}

func owner(r Repo) string {
	o, _, _ := strings.Cut(r.Path, "/")
	return o
}

func stripDocker(s string) string {
	s = strings.ToLower(s)
	s = strings.TrimPrefix(s, "docker-")
	s = strings.TrimSuffix(s, "-docker")
	return s
}

// nameMatch accepts equal names, or one being the other plus a dash suffix (portainer-ee runs portainer).
func nameMatch(repo, image string) bool {
	a, b := stripDocker(repo), stripDocker(image)
	return a == b || strings.HasPrefix(b, a+"-") || strings.HasPrefix(a, b+"-")
}

// ownerMatch accepts the Hub namespace as a prefix of the forge owner or the reverse (umap and umap-project).
func ownerMatch(forgeOwner, ns string) bool {
	a, b := strings.ToLower(forgeOwner), strings.ToLower(ns)
	if a == b {
		return true
	}
	if len(a) < 4 || len(b) < 4 {
		return false
	}
	return strings.HasPrefix(a, b) || strings.HasPrefix(b, a)
}

type githubRepoInfo struct {
	FullName string `json:"full_name"`
	Archived bool   `json:"archived"`
	Stars    int    `json:"stargazers_count"`
}

func (f *fetcher) githubRepo(ctx context.Context, path string) (githubRepoInfo, error) {
	var info githubRepoInfo
	err := f.getJSON(ctx, "https://api.github.com/repos/"+path, &info)
	return info, err
}

func (f *fetcher) githubSearch(ctx context.Context, q string) ([]githubRepoInfo, error) {
	var res struct {
		Items []githubRepoInfo `json:"items"`
	}
	u := "https://api.github.com/search/repositories?per_page=30&q=" + url.QueryEscape(q)
	err := f.getJSON(ctx, u, &res)
	return res.Items, err
}
