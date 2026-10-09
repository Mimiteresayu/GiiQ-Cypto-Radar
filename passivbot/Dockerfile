# C48-1 Passivbot Hyperliquid paper/small-size image.
# Mirrors upstream Dockerfile_live at a pinned commit of enarjord/passivbot (Unlicense),
# then wraps upstream container/entrypoint.sh with the C48-1 lock guard.
# Build context: this passivbot/ directory (Railway root directory = passivbot).

ARG PASSIVBOT_REPO=https://github.com/enarjord/passivbot.git
# master @ 2026-10-04, merge of PR #1873
ARG PASSIVBOT_SHA=4e15b572f06b354411fbcf377fd9b25b7feb024c

# Stage 1: fetch upstream at the pinned SHA and build the live wheel (as Dockerfile_live)
FROM python:3.12-slim-bookworm AS builder
ARG PASSIVBOT_REPO
ARG PASSIVBOT_SHA

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/root/.cargo/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    pkg-config \
    libssl-dev \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y

WORKDIR /upstream
RUN git init -q . \
    && git remote add origin "$PASSIVBOT_REPO" \
    && git fetch -q --depth 1 origin "$PASSIVBOT_SHA" \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$PASSIVBOT_SHA"

RUN pip install --no-cache-dir -r requirements-rust.txt setuptools-rust wheel \
    && python -m pip wheel . --no-deps --no-build-isolation -w /tmp/wheels

# Stage 2: runtime (as Dockerfile_live) + C48-1 config and guard
FROM python:3.12-slim-bookworm
ARG PASSIVBOT_SHA

ENV DEBIAN_FRONTEND=noninteractive
ENV SKIP_RUST_COMPILE=true
ENV PASSIVBOT_SHA=${PASSIVBOT_SHA}
ENV PB_USER=hyperliquid_01
ENV PB_CONFIG_PATH=/app/giiq/configs/giiq_c48_1_paper.json
ENV PB_LOG_DIR=/data/logs
ENV PB_MONITOR_ROOT=/data/monitor

WORKDIR /app

COPY --from=builder /upstream/broker_codes.hjson broker_codes.hjson
COPY --from=builder /upstream/container container
COPY --from=builder /tmp/wheels/*.whl /tmp/wheels/

RUN pip install --no-cache-dir /tmp/wheels/passivbot-*.whl \
    && chmod +x /app/container/entrypoint.sh \
    && rm -rf /tmp/* ~/.cache/pip

COPY configs/giiq_c48_1_paper.json /app/giiq/configs/giiq_c48_1_paper.json
COPY giiq_entrypoint.py /app/giiq/giiq_entrypoint.py

RUN python3 /app/giiq/giiq_entrypoint.py --check-config

ENTRYPOINT ["python3", "/app/giiq/giiq_entrypoint.py"]
