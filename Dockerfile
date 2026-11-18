# syntax=docker/dockerfile:1
FROM node:24-trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir . \
    && groupadd --system --gid 10001 provider \
    && useradd --system --uid 10001 --gid provider --home /app provider \
    && mkdir -p /data /media \
    && chown provider:provider /data /media

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    YSP_DATA_DIR=/data \
    YSP_OUTPUT_ROOT=/media \
    YSP_API_HOST=0.0.0.0 \
    YSP_ADMIN_HOST=0.0.0.0

USER provider
EXPOSE 3030 3031
VOLUME ["/data", "/media"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3030/health', timeout=3)" || exit 1
CMD ["youtube-song-provider", "serve"]
