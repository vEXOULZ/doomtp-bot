# syntax=docker/dockerfile:1.7

# ── Build: resolve and install dependencies into a venv with uv ────────────────
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv venv /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    LOG_FORMAT=json
RUN groupadd --system --gid 10001 bot && useradd --system --uid 10001 --gid bot --no-create-home bot \
    && mkdir -p /data && chown bot:bot /data
COPY --from=build /opt/venv /opt/venv
USER bot
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["doomtp-bot"]
