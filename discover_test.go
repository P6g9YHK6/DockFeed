package main

import "testing"

func TestNameMatch(t *testing.T) {
	yes := [][2]string{{"portainer", "portainer-ee"}, {"docker-autoheal", "autoheal"}, {"umap", "umap"}, {"duplicacy-web", "duplicacy-web-docker"}}
	no := [][2]string{{"online", "code"}, {"portainer-docs", "portainer-ee"}, {"umap", "umapper"}}
	for _, p := range yes {
		if !nameMatch(p[0], p[1]) {
			t.Errorf("nameMatch(%q, %q) = false", p[0], p[1])
		}
	}
	for _, p := range no {
		if nameMatch(p[0], p[1]) {
			t.Errorf("nameMatch(%q, %q) = true", p[0], p[1])
		}
	}
}

func TestOwnerMatch(t *testing.T) {
	yes := [][2]string{{"umap-project", "umap"}, {"CollaboraOnline", "collabora"}, {"Portainer", "portainer"}}
	no := [][2]string{{"lmcinnes", "umap"}, {"dirtsimple", "analogic"}, {"golang", "go"}}
	for _, p := range yes {
		if !ownerMatch(p[0], p[1]) {
			t.Errorf("ownerMatch(%q, %q) = false", p[0], p[1])
		}
	}
	for _, p := range no {
		if ownerMatch(p[0], p[1]) {
			t.Errorf("ownerMatch(%q, %q) = true", p[0], p[1])
		}
	}
}

func TestRank(t *testing.T) {
	cs := []candidate{
		{repo: Repo{"github.com", "CollaboraOnline/online.mirror"}, owner: true, stars: 10, mentions: 2},
		{repo: Repo{"github.com", "CollaboraOnline/online"}, owner: true, stars: 2000, mentions: 1},
		{repo: Repo{"github.com", "other/code"}, name: true, archived: true},
	}
	rank(cs)
	if got := cs[0].repo.Path; got != "other/code" {
		t.Errorf("name match should win, got %s", got)
	}
	if got := cs[1].repo.Path; got != "CollaboraOnline/online" {
		t.Errorf("stars should break ties, got %s", got)
	}
}
