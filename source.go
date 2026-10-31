package main

import (
	"net/url"
	"regexp"
	"strings"
)

const labelPrefix = "dockfeed."

// Repo identifies a source repository on a forge, e.g. github.com + "immich-app/immich".
type Repo struct {
	Host string
	Path string
}

func (r Repo) URL() string    { return "https://" + r.Host + "/" + r.Path }
func (r Repo) String() string { return r.Host + "/" + r.Path }

// Name is the last path segment, used to label items shared by several containers.
func (r Repo) Name() string { return r.Path[strings.LastIndex(r.Path, "/")+1:] }

// officialImages maps Docker Hub library images (no namespace) to their upstream repos.
// They carry no OCI source label, so without this most databases and base images would go unresolved.
var officialImages = map[string]string{
	"alpine":        "https://github.com/alpinelinux/docker-alpine",
	"caddy":         "https://github.com/caddyserver/caddy",
	"docker":        "https://github.com/moby/moby",
	"elasticsearch": "https://github.com/elastic/elasticsearch",
	"ghost":         "https://github.com/TryGhost/Ghost",
	"golang":        "https://github.com/golang/go",
	"haproxy":       "https://github.com/haproxy/haproxy",
	"httpd":         "https://github.com/apache/httpd",
	"mariadb":       "https://github.com/MariaDB/server",
	"memcached":     "https://github.com/memcached/memcached",
	"mongo":         "https://github.com/mongodb/mongo",
	"mysql":         "https://github.com/mysql/mysql-server",
	"nextcloud":     "https://github.com/nextcloud/server",
	"nginx":         "https://github.com/nginx/nginx",
	"node":          "https://github.com/nodejs/node",
	"php":           "https://github.com/php/php-src",
	"postgres":      "https://github.com/postgres/postgres",
	"python":        "https://github.com/python/cpython",
	"rabbitmq":      "https://github.com/rabbitmq/rabbitmq-server",
	"redis":         "https://github.com/redis/redis",
	"registry":      "https://github.com/distribution/distribution",
	"traefik":       "https://github.com/traefik/traefik",
	"valkey":        "https://github.com/valkey-io/valkey",
	"wordpress":     "https://github.com/WordPress/WordPress",
}

// Hosts that serve repositories directly, so an image.url label pointing there is a usable source.
var knownForges = map[string]bool{
	"github.com": true, "gitlab.com": true, "codeberg.org": true,
	"code.forgejo.org": true, "gitea.com": true,
}

var scpLike = regexp.MustCompile(`^git@([^:]+):(.+)$`)

// parseRepoURL normalizes the many spellings found in image labels into a Repo.
func parseRepoURL(raw string) (Repo, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Repo{}, false
	}
	if m := scpLike.FindStringSubmatch(raw); m != nil {
		raw = "https://" + m[1] + "/" + m[2]
	}
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return Repo{}, false
	}
	host := strings.ToLower(strings.TrimPrefix(u.Host, "www."))
	path := strings.Trim(u.Path, "/")
	path = strings.TrimSuffix(path, ".git")
	// Drop deep links like /tree/main/docker or /-/blob/... down to owner/repo.
	if i := strings.Index(path, "/-/"); i >= 0 {
		path = path[:i]
	}
	parts := strings.Split(path, "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		return Repo{}, false
	}
	if host != "gitlab.com" {
		parts = parts[:2]
	}
	return Repo{Host: host, Path: strings.Join(parts, "/")}, true
}

// splitImage returns registry, repository path and tag of an image reference.
func splitImage(image string) (registry, path, tag string) {
	if i := strings.Index(image, "@"); i >= 0 {
		image = image[:i]
	}
	if i := strings.LastIndex(image, ":"); i > strings.LastIndex(image, "/") {
		image, tag = image[:i], image[i+1:]
	}
	first, rest, ok := strings.Cut(image, "/")
	if ok && (strings.ContainsAny(first, ".:") || first == "localhost") {
		registry, path = first, rest
	} else {
		registry, path = "docker.io", image
	}
	if registry == "docker.io" || registry == "index.docker.io" {
		registry = "docker.io"
		path = strings.TrimPrefix(path, "library/")
	}
	return registry, path, tag
}

// resolveRepo works out where a container's code lives, most explicit hint first.
func resolveRepo(c Container) (Repo, string, bool) {
	try := func(raw, via string) (Repo, string, bool) {
		r, ok := parseRepoURL(raw)
		return r, via, ok
	}
	if v := c.Labels[labelPrefix+"source"]; v != "" {
		return try(v, "label "+labelPrefix+"source")
	}
	for _, l := range []string{"org.opencontainers.image.source", "org.label-schema.vcs-url"} {
		if r, via, ok := try(c.Labels[l], l); ok {
			return r, via, ok
		}
	}
	for _, l := range []string{"org.opencontainers.image.url", "org.label-schema.url"} {
		if r, ok := parseRepoURL(c.Labels[l]); ok && knownForges[r.Host] {
			return r, l, true
		}
	}

	registry, path, _ := splitImage(c.Image)
	switch {
	case registry == "ghcr.io":
		return try("https://github.com/"+path, "ghcr.io image name")
	case registry == "codeberg.org" || registry == "code.forgejo.org" || registry == "data.forgejo.org":
		host := registry
		if host == "data.forgejo.org" {
			host = "code.forgejo.org"
		}
		return try("https://"+host+"/"+path, registry+" image name")
	case registry == "registry.gitlab.com":
		return try("https://gitlab.com/"+path, "registry.gitlab.com image name")
	case registry == "lscr.io" || (registry == "docker.io" && strings.HasPrefix(path, "linuxserver/")):
		return try("https://github.com/linuxserver/docker-"+strings.TrimPrefix(path, "linuxserver/"), "linuxserver image name")
	case registry == "docker.io" && !strings.Contains(path, "/"):
		if u, ok := officialImages[path]; ok {
			return try(u, "official image map")
		}
	}
	return Repo{}, "", false
}

// guessRepos lists likely GitHub repos for a Docker Hub image named owner/name.
// They are only candidates: the caller must check they exist before using one.
func guessRepos(image string) []Repo {
	registry, path, _ := splitImage(image)
	owner, name, ok := strings.Cut(path, "/")
	if registry != "docker.io" || !ok || strings.Contains(name, "/") {
		return nil
	}
	repos := []Repo{{Host: "github.com", Path: owner + "/" + name}}
	if !strings.HasPrefix(name, "docker-") {
		repos = append(repos, Repo{Host: "github.com", Path: owner + "/docker-" + name})
	}
	return repos
}
