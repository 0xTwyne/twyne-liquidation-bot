ARG PYTHON_VERSION=3.13

# ---------- Stage 1: builder ----------
# Full Debian-based Python image — has the toolchain (gcc, libc-dev, etc.)
# that uv may need to compile any source-only Python dependencies.
FROM python:${PYTHON_VERSION} AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Resolve and install dependencies first, separately from the source tree.
# This layer caches across source-only changes, making warm rebuilds fast.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the project and install it.
COPY . /app
RUN mkdir -p /app/logs /app/state \
    && uv sync --frozen --no-dev

# ---------- Stage 2: final runtime image ----------
# Slim base — no gcc / git / build tools. ~900 MB lighter than the full image.
FROM python:${PYTHON_VERSION}-slim

# Minimal shared libraries that web3 / eth_account / coincurve dynamically
# link against at runtime. If `flask run` ever errors at import time with a
# missing .so, add the corresponding libXxx package here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        libffi8 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-privileged user. UID 1000 matches the host-side ownership of the
# volume-mounted logs/ and state/ directories (see Makefile run-docker).
ARG UID=1000
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

WORKDIR /app

# --chown sets ownership at copy time, so we don't need a separate
# `chown -R` layer (which would duplicate /app into a new overlay layer).
COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH"
ENV FLASK_APP=application
USER appuser
EXPOSE 8080

# Single worker: this is a stateful app — multiple workers would spin up
# competing chain monitors signing with the same EOA (see B21 in the review).
# Use --timeout 120 so slow RPC calls do not kill worker health checks.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120", "application:application"]
