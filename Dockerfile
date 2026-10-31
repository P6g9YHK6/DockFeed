FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# requirements.lock is the hash-pinned resolution of requirements.txt, so a
# rebuild of the same commit always installs the exact same dependencies.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY dockfeed ./dockfeed

ARG VERSION=dev
ENV DOCKFEED_VERSION=$VERSION

EXPOSE 8080
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s CMD ["python", "-m", "dockfeed", "--healthcheck"]
ENTRYPOINT ["python", "-m", "dockfeed"]
