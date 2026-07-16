FROM node:20-bookworm-slim
ARG CODEX_CLI_VERSION
RUN test -n "$CODEX_CLI_VERSION" \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 python3-pip \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY app /app/app
RUN useradd --create-home --uid 10001 runtime && mkdir -p /worktrees && chown runtime:runtime /worktrees
USER runtime
CMD ["python3", "-m", "app.main"]
