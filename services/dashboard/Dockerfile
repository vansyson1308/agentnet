FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Production WSGI server (gunicorn.conf.py): binds 0.0.0.0:$PORT (default 8080),
# bounded gthread workers, graceful shutdown on SIGTERM. Exec form so gunicorn
# is PID 1 and receives the platform's stop signal directly.
EXPOSE 8080

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app.main:app"]
