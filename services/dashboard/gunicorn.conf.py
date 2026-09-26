"""Production WSGI server for the dashboard (replaces Flask's development server).

The image runs ``gunicorn --config gunicorn.conf.py app.main:app``. Everything
the application sees is unchanged: same port, same ``/healthz``, and proxy
headers are still interpreted only by the app's ``ProxyFix`` (``BEHIND_PROXY``),
never by gunicorn itself (``forwarded_allow_ips`` keeps its loopback default).

Sizing is for one small container (Railway Hobby): a few threaded workers are
plenty for server-rendered pages that call the registry over the private
network. Both knobs are environment-tunable and hard-capped so a stray value
cannot fork-bomb the container.
"""

import os


def _bounded_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, value))


bind = f"0.0.0.0:{_bounded_int('PORT', 8080, 1, 65535)}"
worker_class = "gthread"
workers = _bounded_int("GUNICORN_WORKERS", 2, 1, 4)
threads = _bounded_int("GUNICORN_THREADS", 4, 1, 16)

# A page render waits on the registry (2-10 s client timeouts in api_client.py);
# 30 s is generous for that and still reaps a wedged worker.
timeout = 30
# SIGTERM from the platform: stop accepting, let in-flight requests finish.
graceful_timeout = 20
keepalive = 5

# Bounded request surface (the dashboard only serves pages and small forms).
limit_request_line = 8190
limit_request_fields = 100
limit_request_field_size = 8190

# Recycle workers now and then so slow leaks cannot accumulate.
max_requests = 2000
max_requests_jitter = 200

# Access log to stdout WITHOUT the query string (%(U)s is the path only), so a
# token or code in a URL can never reach the log stream.
accesslog = "-"
access_log_format = '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms'
errorlog = "-"
loglevel = "info"
