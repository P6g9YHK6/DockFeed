package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var version = "dev"

type config struct {
	DockerHost     string
	Listen         string
	Interval       time.Duration
	StaleAfter     time.Duration
	ItemsPerRepo   int
	MaxItems       int
	Concurrency    int
	FeedTitle      string
	IncludeStopped bool
	Discover       bool
	GitHubToken    string
	Exclude        *regexp.Regexp
	PublicURL      string
}

func env(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func loadConfig() (config, error) {
	c := config{
		DockerHost: env("DOCKER_HOST", "unix:///var/run/docker.sock"),
		Listen:     env("LISTEN_ADDR", ":8080"),
		FeedTitle:  env("FEED_TITLE", "DockFeed"),
		PublicURL:  strings.TrimRight(env("PUBLIC_URL", ""), "/"),
	}
	var err error
	durations := []struct {
		dst *time.Duration
		key string
		def string
	}{
		{&c.Interval, "REFRESH_INTERVAL", "1h"},
		{&c.StaleAfter, "STALE_RELEASE_AFTER", "8760h"},
	}
	for _, d := range durations {
		if *d.dst, err = time.ParseDuration(env(d.key, d.def)); err != nil {
			return c, fmt.Errorf("%s: %w", d.key, err)
		}
	}
	ints := []struct {
		dst *int
		key string
		def string
	}{
		{&c.ItemsPerRepo, "ITEMS_PER_REPO", "10"},
		{&c.MaxItems, "MAX_ITEMS", "500"},
		{&c.Concurrency, "CONCURRENCY", "8"},
	}
	for _, i := range ints {
		if *i.dst, err = strconv.Atoi(env(i.key, i.def)); err != nil || *i.dst < 1 {
			return c, fmt.Errorf("%s must be a positive integer", i.key)
		}
	}
	if c.IncludeStopped, err = strconv.ParseBool(env("INCLUDE_STOPPED", "false")); err != nil {
		return c, fmt.Errorf("INCLUDE_STOPPED: %w", err)
	}
	if c.Discover, err = strconv.ParseBool(env("DISCOVER", "true")); err != nil {
		return c, fmt.Errorf("DISCOVER: %w", err)
	}
	c.GitHubToken = env("GITHUB_TOKEN", "")
	if v := env("EXCLUDE", ""); v != "" {
		if c.Exclude, err = regexp.Compile(v); err != nil {
			return c, fmt.Errorf("EXCLUDE: %w", err)
		}
	}
	return c, nil
}

// target is one repo to follow plus every container that runs code from it.
type target struct {
	Repo       Repo
	Via        string
	Mode       string
	Branch     string
	Containers []string
	Running    string
	Include    *regexp.Regexp
	Exclude    *regexp.Regexp
	Err        error
}

// keep applies the include/exclude title filters; a filtered-out release doesn't count as one.
func (t *target) keep(items []Item, global *regexp.Regexp) []Item {
	exclude := t.Exclude
	if exclude == nil {
		exclude = global
	}
	var out []Item
	for _, it := range items {
		if t.Include != nil && !t.Include.MatchString(it.Title) {
			continue
		}
		if exclude != nil && exclude.MatchString(it.Title) {
			continue
		}
		out = append(out, it)
	}
	return out
}

type repoStatus struct {
	Repo       string    `json:"repo"`
	Via        string    `json:"resolved_via"`
	Containers []string  `json:"containers"`
	Kind       string    `json:"kind,omitempty"`
	Items      int       `json:"items"`
	Latest     time.Time `json:"latest,omitempty"`
	Error      string    `json:"error,omitempty"`
}

type status struct {
	Version    string            `json:"version"`
	LastScan   time.Time         `json:"last_scan"`
	ScanError  string            `json:"scan_error,omitempty"`
	Repos      []repoStatus      `json:"repos"`
	Unresolved map[string]string `json:"unresolved"`
	Ignored    []string          `json:"ignored"`
}

type app struct {
	cfg     config
	docker  *dockerClient
	fetcher *fetcher
	disc    *discoverer

	mu      sync.RWMutex
	entries []FeedEntry
	status  status
	ready   bool
}

func (a *app) targets(ctx context.Context, cs []Container) (map[string]*target, map[string]string, []string) {
	targets := map[string]*target{}
	unresolved := map[string]string{}
	ignored := []string{}
	for _, c := range cs {
		if v, ok := c.Labels[labelPrefix+"enable"]; ok {
			if on, err := strconv.ParseBool(v); err == nil && !on {
				ignored = append(ignored, c.Name)
				continue
			}
		}
		repo, via, ok := resolveRepo(c)
		if !ok && a.cfg.Discover {
			repo, via, ok = a.disc.discover(ctx, c.Image)
		}
		if !ok {
			unresolved[c.Name] = c.Image
			continue
		}
		t := targets[repo.String()]
		if t == nil {
			t = &target{Repo: repo, Via: via, Mode: "auto"}
			targets[repo.String()] = t
		}
		t.Containers = append(t.Containers, c.Name)
		if m := c.Labels[labelPrefix+"mode"]; m != "" {
			t.Mode = m
		}
		if b := c.Labels[labelPrefix+"branch"]; b != "" {
			t.Branch = b
		}
		for _, f := range []struct {
			dst **regexp.Regexp
			key string
		}{{&t.Include, "include"}, {&t.Exclude, "exclude"}} {
			if v := c.Labels[labelPrefix+f.key]; v != "" {
				re, err := regexp.Compile(v)
				if err != nil {
					t.Err = fmt.Errorf("%s on %s: %w", labelPrefix+f.key, c.Name, err)
					continue
				}
				*f.dst = re
			}
		}
		if v := c.Labels["org.opencontainers.image.version"]; v != "" && t.Running == "" {
			t.Running = v
		}
	}
	for _, t := range targets {
		sort.Strings(t.Containers)
	}
	sort.Strings(ignored)
	return targets, unresolved, ignored
}

// collect fetches one target, falling back to commits when the repo doesn't publish releases.
func (a *app) collect(ctx context.Context, t *target) ([]Item, string, error) {
	if t.Err != nil {
		return nil, "", t.Err
	}
	switch t.Mode {
	case "releases":
		items, err := a.fetcher.releases(ctx, t.Repo)
		return t.keep(items, a.cfg.Exclude), "release", err
	case "commits":
		items, err := a.fetcher.commits(ctx, t.Repo, t.Branch)
		return t.keep(items, a.cfg.Exclude), "commit", err
	case "auto":
	default:
		return nil, "", fmt.Errorf("unknown %smode %q", labelPrefix, t.Mode)
	}
	items, err := a.fetcher.releases(ctx, t.Repo)
	if err != nil && !errors.Is(err, errNotFound) {
		return nil, "", err
	}
	items = t.keep(items, a.cfg.Exclude)
	if len(items) > 0 && (a.cfg.StaleAfter == 0 || time.Since(items[0].Updated) < a.cfg.StaleAfter) {
		return items, "release", nil
	}
	commits, cerr := a.fetcher.commits(ctx, t.Repo, t.Branch)
	if cerr != nil {
		if len(items) > 0 {
			return items, "release", nil
		}
		return nil, "", cerr
	}
	return t.keep(commits, a.cfg.Exclude), "commit", nil
}

func (a *app) refresh(ctx context.Context) {
	start := time.Now()
	cs, err := a.docker.Containers(ctx, a.cfg.IncludeStopped)
	if err != nil {
		slog.Error("docker scan failed", "err", err)
		a.mu.Lock()
		a.status.ScanError = err.Error()
		a.mu.Unlock()
		return
	}
	targets, unresolved, ignored := a.targets(ctx, cs)

	var (
		wg      sync.WaitGroup
		mu      sync.Mutex
		entries []FeedEntry
		repos   []repoStatus
		sem     = make(chan struct{}, a.cfg.Concurrency)
	)
	for _, t := range targets {
		wg.Add(1)
		go func(t *target) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			cctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
			defer cancel()
			items, kind, err := a.collect(cctx, t)
			rs := repoStatus{Repo: t.Repo.String(), Via: t.Via, Containers: t.Containers, Kind: kind}
			if err != nil {
				rs.Error = err.Error()
				slog.Warn("fetch failed", "repo", t.Repo.String(), "err", err)
			}
			if len(items) > a.cfg.ItemsPerRepo {
				items = items[:a.cfg.ItemsPerRepo]
			}
			label := t.Repo.Name()
			if len(t.Containers) == 1 {
				label = t.Containers[0]
			}
			var es []FeedEntry
			for _, it := range items {
				es = append(es, FeedEntry{Item: it, Label: label, Kind: kind, Containers: t.Containers, Running: t.Running, Repo: t.Repo})
			}
			rs.Items = len(items)
			if len(items) > 0 {
				rs.Latest = items[0].Updated
			}
			mu.Lock()
			entries = append(entries, es...)
			repos = append(repos, rs)
			mu.Unlock()
		}(t)
	}
	wg.Wait()

	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Updated.After(entries[j].Updated) })
	if len(entries) > a.cfg.MaxItems {
		entries = entries[:a.cfg.MaxItems]
	}
	sort.Slice(repos, func(i, j int) bool { return repos[i].Repo < repos[j].Repo })

	a.mu.Lock()
	a.entries = entries
	a.status = status{Version: version, LastScan: start, Repos: repos, Unresolved: unresolved, Ignored: ignored}
	a.ready = true
	a.mu.Unlock()
	slog.Info("refreshed", "containers", len(cs), "repos", len(targets), "unresolved", len(unresolved), "items", len(entries), "took", time.Since(start).Round(time.Millisecond))
}

func (a *app) handleFeed(w http.ResponseWriter, r *http.Request) {
	a.mu.RLock()
	entries, ready := a.entries, a.ready
	a.mu.RUnlock()
	if !ready {
		http.Error(w, "first scan still running", http.StatusServiceUnavailable)
		return
	}
	// ?container=a,b narrows the feed; ?kind=release drops commit fallbacks.
	if q := r.URL.Query().Get("container"); q != "" {
		want := map[string]bool{}
		for _, n := range strings.Split(q, ",") {
			want[strings.TrimSpace(n)] = true
		}
		entries = filter(entries, func(e FeedEntry) bool {
			for _, c := range e.Containers {
				if want[c] {
					return true
				}
			}
			return false
		})
	}
	if k := r.URL.Query().Get("kind"); k != "" {
		entries = filter(entries, func(e FeedEntry) bool { return e.Kind == k })
	}
	self := a.cfg.PublicURL
	if self == "" {
		scheme := "http"
		if r.TLS != nil || r.Header.Get("X-Forwarded-Proto") == "https" {
			scheme = "https"
		}
		self = scheme + "://" + r.Host
	}
	var buf bytes.Buffer
	if err := renderAtom(&buf, a.cfg.FeedTitle, self+r.URL.RequestURI(), entries); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/atom+xml; charset=utf-8")
	_, _ = w.Write(buf.Bytes())
}

func filter(in []FeedEntry, keep func(FeedEntry) bool) []FeedEntry {
	var out []FeedEntry
	for _, e := range in {
		if keep(e) {
			out = append(out, e)
		}
	}
	return out
}

func (a *app) handleStatus(w http.ResponseWriter, _ *http.Request) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(a.status)
}

func healthcheck(listen string) int {
	host, port, err := net.SplitHostPort(listen)
	if err != nil {
		return 1
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+net.JoinHostPort(host, port)+"/healthz", nil)
	if err != nil {
		return 1
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func main() {
	os.Exit(run())
}

func run() int {
	hc := flag.Bool("healthcheck", false, "probe the running server and exit 0 if healthy")
	once := flag.Bool("once", false, "scan once, print the feed to stdout and exit")
	flag.Parse()

	cfg, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	if *hc {
		return healthcheck(cfg.Listen)
	}
	docker, err := newDockerClient(cfg.DockerHost)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	f := newFetcher("dockfeed/"+version+" (+https://github.com/P6g9YHK6/DockFeed)", cfg.GitHubToken)
	a := &app{cfg: cfg, docker: docker, fetcher: f, disc: newDiscoverer(f)}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *once {
		a.refresh(ctx)
		if err := renderAtom(os.Stdout, cfg.FeedTitle, "urn:stdout", a.entries); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return 0
	}

	go func() {
		a.refresh(ctx)
		t := time.NewTicker(cfg.Interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				a.refresh(ctx)
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", a.handleFeed)
	mux.HandleFunc("GET /feed.atom", a.handleFeed)
	mux.HandleFunc("GET /status", a.handleStatus)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) })

	srv := &http.Server{Addr: cfg.Listen, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	slog.Info("listening", "addr", cfg.Listen, "docker", cfg.DockerHost, "interval", cfg.Interval)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server", "err", err)
		return 1
	}
	return 0
}
