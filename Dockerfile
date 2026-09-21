# syntax=docker/dockerfile:1.7

# ── Build: install the locked dependency set into a venv with uv ───────────────
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never     UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
# Dependencies first, so a source-only change doesn't re-resolve or re-download them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
# The language page shows the grammar, and the wheel force-includes it (pyproject) — so it has to be here.
COPY docs/grammar/railroad.ebnf ./docs/grammar/railroad.ebnf
RUN uv sync --locked --no-dev --no-editable

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
# The one-shot tools (backup, coverage, starter pack) run from the image, not from a checkout beside it:
# a guest that deploys by pulling has no source tree to mount over them.
COPY scripts /app/scripts
USER bot
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["doomtp-bot"]
