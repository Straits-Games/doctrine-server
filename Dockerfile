# doctrine-server container image (includes the optional Discord plugin).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOCTRINE_HOST=0.0.0.0 \
    DOCTRINE_PORT=8080 \
    DOCTRINE_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY doctrine_server ./doctrine_server
RUN pip install ".[discord]" \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin doctrine \
    && mkdir -p /data && chown doctrine:doctrine /data

USER doctrine
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('DOCTRINE_PORT', '8080'), timeout=4)"]

ENTRYPOINT ["doctrine-server"]
CMD ["serve"]
