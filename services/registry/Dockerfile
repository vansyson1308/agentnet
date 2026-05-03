FROM python:3.10-slim

WORKDIR /app

# Install build dependencies for ed25519 + psycopg2.
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies (alembic added explicitly so migrations run on startup).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir alembic==1.13.1

# Copy application code + alembic config + entrypoint.
COPY app /app/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
