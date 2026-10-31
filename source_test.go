package main

import "testing"

func TestParseRepoURL(t *testing.T) {
	cases := map[string]string{
		"https://github.com/immich-app/immich":             "github.com/immich-app/immich",
		"https://github.com/element-hq/synapse.git":        "github.com/element-hq/synapse",
		"github.com/foo/bar/tree/main/docker":              "github.com/foo/bar",
		"git@github.com:foo/bar.git":                       "github.com/foo/bar",
		"https://www.github.com/Foo/Bar/":                  "github.com/Foo/Bar",
		"https://gitlab.com/group/sub/project/-/tree/main": "gitlab.com/group/sub/project",
		"https://codeberg.org/forgejo/forgejo":             "codeberg.org/forgejo/forgejo",
	}
	for in, want := range cases {
		r, ok := parseRepoURL(in)
		if !ok || r.String() != want {
			t.Errorf("parseRepoURL(%q) = %q, %v; want %q", in, r.String(), ok, want)
		}
	}
	for _, in := range []string{"", "https://www.portainer.io", "https://github.com/onlyowner"} {
		if r, ok := parseRepoURL(in); ok {
			t.Errorf("parseRepoURL(%q) = %q; want no match", in, r.String())
		}
	}
}

func TestSplitImage(t *testing.T) {
	cases := []struct{ in, reg, path, tag string }{
		{"redis", "docker.io", "redis", ""},
		{"library/postgres:18-alpine", "docker.io", "postgres", "18-alpine"},
		{"ghcr.io/immich-app/immich-server:v3", "ghcr.io", "immich-app/immich-server", "v3"},
		{"localhost:5000/me/app:dev", "localhost:5000", "me/app", "dev"},
		{"grafana/loki@sha256:abc", "docker.io", "grafana/loki", ""},
	}
	for _, c := range cases {
		reg, path, tag := splitImage(c.in)
		if reg != c.reg || path != c.path || tag != c.tag {
			t.Errorf("splitImage(%q) = %q %q %q", c.in, reg, path, tag)
		}
	}
}

func TestResolveRepo(t *testing.T) {
	cases := []struct {
		image  string
		labels map[string]string
		want   string
	}{
		{"whatever", map[string]string{"dockfeed.source": "https://github.com/a/b", "org.opencontainers.image.source": "https://github.com/c/d"}, "github.com/a/b"},
		{"whatever", map[string]string{"org.opencontainers.image.source": "https://github.com/c/d"}, "github.com/c/d"},
		{"whatever", map[string]string{"org.opencontainers.image.url": "https://www.portainer.io"}, ""},
		{"whatever", map[string]string{"org.opencontainers.image.url": "https://github.com/e/f"}, "github.com/e/f"},
		{"ghcr.io/owner/app:latest", nil, "github.com/owner/app"},
		{"lscr.io/linuxserver/sonarr", nil, "github.com/linuxserver/docker-sonarr"},
		{"data.forgejo.org/forgejo/runner:12", nil, "code.forgejo.org/forgejo/runner"},
		{"redis:alpine", nil, "github.com/redis/redis"},
		{"someone/thing", nil, ""},
	}
	for _, c := range cases {
		r, _, ok := resolveRepo(Container{Image: c.image, Labels: c.labels})
		got := ""
		if ok {
			got = r.String()
		}
		if got != c.want {
			t.Errorf("resolveRepo(%q, %v) = %q; want %q", c.image, c.labels, got, c.want)
		}
	}
}

func TestGuessRepos(t *testing.T) {
	got := guessRepos("willfarrell/autoheal:latest")
	if len(got) != 2 || got[0].String() != "github.com/willfarrell/autoheal" || got[1].String() != "github.com/willfarrell/docker-autoheal" {
		t.Errorf("guessRepos = %v", got)
	}
	for _, img := range []string{"redis", "ghcr.io/a/b", "lscr.io/linuxserver/x"} {
		if g := guessRepos(img); g != nil {
			t.Errorf("guessRepos(%q) = %v; want none", img, g)
		}
	}
}
