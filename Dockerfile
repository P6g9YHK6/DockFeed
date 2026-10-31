# The runtime is a scratch image holding only the parts of CPython DockFeed
# needs, see docker/slim.sh.
FROM python:3.14-alpine AS build
# binutils is only for strip in slim.sh and never reaches the final image; pinning
# an Alpine package version breaks on every Alpine point release.
# hadolint ignore=DL3018
RUN apk add --no-cache binutils
COPY requirements.lock /tmp/
RUN pip install --no-cache-dir --require-hashes --target /deps -r /tmp/requirements.lock
COPY dockfeed /app/dockfeed
COPY docker/slim.sh /usr/local/bin/slim.sh
RUN sh /usr/local/bin/slim.sh

# Fails the build if slim.sh pruned something DockFeed uses.
FROM scratch AS smoke
COPY --from=build /rootfs /
COPY docker/smoke.py /smoke.py
RUN ["/usr/local/bin/python", "/smoke.py"]

FROM scratch
COPY --from=build /rootfs /
# Forces the smoke stage to run; buildx skips stages nothing depends on.
COPY --from=smoke /smoke-ok /etc/dockfeed-smoke-ok
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ARG VERSION=dev
ENV DOCKFEED_VERSION=$VERSION
EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s CMD ["/usr/local/bin/python", "-m", "dockfeed", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/python", "-m", "dockfeed"]
