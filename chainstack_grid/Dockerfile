# C48-3 Chainstack Hyperliquid grid bot, HL TESTNET ONLY.
# Build context: this chainstack_grid/ directory (Railway Root Directory = chainstack_grid).
# No VOLUME instruction: Railway rejects it; attach a Railway Volume at /data instead.

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM python:3.13-slim@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f AS build

# chainstacklabs/hyperliquid-trading-bot main @ 2026-06-29 (see README "Upstream pin").
ARG UPSTREAM_REPO=https://github.com/chainstacklabs/hyperliquid-trading-bot.git
ARG UPSTREAM_SHA=7e930aa6b0d296f029943834c2133beff6174d80

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN git init -q /app/upstream \
    && cd /app/upstream \
    && git fetch -q --depth 1 "$UPSTREAM_REPO" "$UPSTREAM_SHA" \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$UPSTREAM_SHA" \
    && rm -rf .git

WORKDIR /app/upstream
RUN uv sync --frozen

COPY . /app/giiq

# Build fails if a patch anchor drifted, a lock is broken, upstream --validate rejects the
# locked yaml, or any lock / patch / entrypoint test fails.
RUN python3 /app/giiq/apply_patches.py /app/upstream \
    && .venv/bin/python /app/giiq/assert_locks.py /app/giiq/bots/c48_3_btc_grid_locked.yaml \
    && .venv/bin/python src/run_bot.py --validate /app/giiq/bots/c48_3_btc_grid_locked.yaml \
    && cd /app/giiq \
    && GIIQ_UPSTREAM_DIR=/app/upstream /app/upstream/.venv/bin/python -m unittest -v test_chainstack_locks

FROM python:3.13-slim@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f

COPY --from=build /app /app
ENV GIIQ_UPSTREAM_DIR=/app/upstream \
    PYTHONUNBUFFERED=1
WORKDIR /app/upstream

ENTRYPOINT ["/app/upstream/.venv/bin/python", "/app/giiq/giiq_entrypoint.py"]
CMD []
