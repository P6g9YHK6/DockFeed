FROM --platform=$BUILDPLATFORM golang:1.24-alpine AS build
ARG TARGETOS TARGETARCH VERSION=dev
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags "-s -w -X main.version=$VERSION" -o /dockfeed .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=build /dockfeed /dockfeed
EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=10s --start-period=10s CMD ["/dockfeed", "-healthcheck"]
ENTRYPOINT ["/dockfeed"]
