FROM python:3.10-slim

WORKDIR /app

# Install build dependencies for ed25519 + psycopg2, plus git: the society
# worker (same image) creates isolated agentnet-auto/* worktrees with it.
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev libffi-dev git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies (alembic added explicitly so migrations run on startup).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir alembic==1.13.1

# Copy application code + alembic config + entrypoint. init-db/ is copied
# too so the entrypoint can bootstrap an EMPTY database (managed Postgres
# never runs /docker-entrypoint-initdb.d) via `python -m app.db_bootstrap`.
COPY app /app/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY init-db /app/init-db
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
# --proxy-headers lets request.url / request.scheme / request.client follow
# X-Forwarded-* — but ONLY from peers listed in FORWARDED_ALLOW_IPS (uvicorn
# reads that env var; default 127.0.0.1). Set it to your platform's proxy
# address range at deploy time. Never '*' on a directly reachable port: that
# would let any client spoof its IP (rate-limit bypass) and scheme.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
