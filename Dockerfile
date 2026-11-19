# syntax=docker/dockerfile:1
FROM node:24-alpine AS node-runtime

FROM alpine:3.24 AS runtime-base
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates ffmpeg libstdc++ python3 \
        'libcrypto3>=3.5.9-r1' 'libssl3>=3.5.9-r1' \
    && addgroup -S -g 10001 provider \
    && adduser -S -D -u 10001 -G provider -h /app provider \
    && mkdir -p /data /media \
    && chown provider:provider /data /media

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    YSP_DATA_DIR=/data \
    YSP_OUTPUT_ROOT=/media \
    YSP_API_HOST=0.0.0.0 \
    YSP_ADMIN_HOST=0.0.0.0
WORKDIR /app

FROM runtime-base AS dependencies
RUN apk add --no-cache py3-pip \
    && python3 -m venv /opt/venv \
    && python -m pip install --no-cache-dir --upgrade 'pip>=26.2'
COPY pyproject.toml ./
RUN python3 -c 'import tomllib; from pathlib import Path; metadata = tomllib.loads(Path("pyproject.toml").read_text()); Path("/tmp/requirements.txt").write_text("\n".join(metadata["project"]["dependencies"]) + "\n")' \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt

FROM dependencies AS package
COPY README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /tmp/wheels .

FROM runtime-base
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=package /tmp/wheels/ /tmp/wheels/
RUN python -m pip install --no-index --no-deps --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && mkdir -m 0777 /icons

USER provider
EXPOSE 3030 3031
VOLUME ["/data", "/media"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3030/health', timeout=3)" || exit 1
CMD ["youtube-song-provider", "serve"]
