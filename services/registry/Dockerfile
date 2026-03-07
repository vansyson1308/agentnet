FROM python:3.10-slim

WORKDIR /app

# Install build dependencies for ed25519
RUN apt-get update && apt-get install -y --no-install-recommends     gcc     python3-dev     libffi-dev     && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app /app/app

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
