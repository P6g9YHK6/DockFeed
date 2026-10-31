package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Container is the subset of the Docker API container listing we care about.
type Container struct {
	Name   string
	Image  string
	Labels map[string]string
}

type dockerClient struct {
	http *http.Client
	base string
}

// newDockerClient accepts unix:///path/to.sock or tcp://host:port (for socket proxies).
func newDockerClient(host string) (*dockerClient, error) {
	u, err := url.Parse(host)
	if err != nil {
		return nil, fmt.Errorf("invalid DOCKER_HOST %q: %w", host, err)
	}
	switch u.Scheme {
	case "unix":
		path := u.Path
		tr := &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				var d net.Dialer
				return d.DialContext(ctx, "unix", path)
			},
		}
		return &dockerClient{http: &http.Client{Transport: tr, Timeout: 30 * time.Second}, base: "http://docker"}, nil
	case "tcp", "http":
		return &dockerClient{http: &http.Client{Timeout: 30 * time.Second}, base: "http://" + u.Host}, nil
	case "https":
		return &dockerClient{http: &http.Client{Timeout: 30 * time.Second}, base: "https://" + u.Host}, nil
	}
	return nil, fmt.Errorf("unsupported DOCKER_HOST scheme %q", u.Scheme)
}

func (d *dockerClient) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, d.base+path, nil)
	if err != nil {
		return err
	}
	resp, err := d.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("docker %s: %s", path, resp.Status)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (d *dockerClient) Containers(ctx context.Context, all bool) ([]Container, error) {
	var raw []struct {
		Names   []string
		Image   string
		ImageID string
		Labels  map[string]string
	}
	if err := d.get(ctx, fmt.Sprintf("/containers/json?all=%t", all), &raw); err != nil {
		return nil, err
	}
	out := make([]Container, 0, len(raw))
	for _, c := range raw {
		name := ""
		if len(c.Names) > 0 {
			name = strings.TrimPrefix(c.Names[0], "/")
		}
		image := c.Image
		// The listing shows a bare digest when the tag has since moved to a newer image.
		if strings.HasPrefix(image, "sha256:") {
			var img struct{ RepoTags []string }
			if err := d.get(ctx, "/images/"+c.ImageID+"/json", &img); err == nil && len(img.RepoTags) > 0 {
				image = img.RepoTags[0]
			}
		}
		out = append(out, Container{Name: name, Image: image, Labels: c.Labels})
	}
	return out, nil
}
