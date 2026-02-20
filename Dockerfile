ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION} AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy the project files
WORKDIR /app
RUN mkdir -p /app/logs /app/state
COPY . /app

# Install Foundry toolchain
RUN mkdir -p lib/forge-std && \
    git clone https://github.com/foundry-rs/forge-std.git lib/forge-std

RUN curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup

RUN cp -r /root/.foundry/bin/* /usr/local/bin/

# Run Forge commands
RUN forge install --no-git
RUN forge build

# Install Python dependencies
RUN uv sync --frozen --no-dev

# Create a non-privileged user and transfer ownership
ARG UID=1000
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

RUN chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"
ENV FLASK_APP=application
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=8080
USER appuser
EXPOSE 8080

ENTRYPOINT ["flask", "run"]
