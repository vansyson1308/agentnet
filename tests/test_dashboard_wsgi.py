"""The dashboard runs under a production WSGI server, never Flask's dev server.

Production logged "WARNING: This is a development server" from ``flask run``.
These tests pin the replacement: the image starts gunicorn with a bounded
config, and a real gunicorn process serves ``/healthz`` on ``$PORT`` and stops
gracefully on SIGTERM.
"""

import importlib.util
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "services" / "dashboard"


def _load_conf(env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("_gunicorn_conf", DASHBOARD / "gunicorn.conf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_image_starts_gunicorn_not_the_flask_dev_server():
    dockerfile = (DASHBOARD / "Dockerfile").read_text()
    cmd = re.findall(r"^CMD\s+(.*)$", dockerfile, re.M)
    assert cmd == ['["gunicorn", "--config", "gunicorn.conf.py", "app.main:app"]']
    assert "flask run" not in dockerfile and '"flask"' not in dockerfile
    reqs = (DASHBOARD / "requirements.txt").read_text()
    assert re.search(r"^gunicorn==\d+\.\d+\.\d+$", reqs, re.M), "gunicorn must be pinned"


def test_config_binds_port_and_bounds_workers():
    conf = _load_conf({"PORT": "8080", "GUNICORN_WORKERS": "2", "GUNICORN_THREADS": "4"})
    assert conf.bind == "0.0.0.0:8080"
    assert conf.worker_class == "gthread"
    assert (conf.workers, conf.threads) == (2, 4)
    assert conf.graceful_timeout > 0 and conf.timeout > 0
    # An absurd value is clamped, never trusted.
    conf = _load_conf({"PORT": "9999", "GUNICORN_WORKERS": "500", "GUNICORN_THREADS": "junk"})
    assert conf.bind == "0.0.0.0:9999"
    assert conf.workers == 4 and conf.threads == 4
    # Query strings never reach the access log (%(U)s is the path only).
    assert "%(U)s" in conf.access_log_format
    assert "%(r)s" not in conf.access_log_format and "%(q)s" not in conf.access_log_format


def test_config_does_not_widen_proxy_trust():
    """Proxy headers stay the app's job (ProxyFix under BEHIND_PROXY); gunicorn
    keeps its loopback-only default so it never rewrites the scheme itself."""
    conf = _load_conf({})
    assert not hasattr(conf, "forwarded_allow_ips")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_gunicorn_serves_healthz_and_stops_gracefully():
    pytest.importorskip("gunicorn")
    port = _free_port()
    env = dict(os.environ, PORT=str(port), ENVIRONMENT="development", GUNICORN_WORKERS="1", GUNICORN_THREADS="2")
    env.pop("FLASK_SECRET_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "--config", "gunicorn.conf.py", "app.main:app"],
        cwd=DASHBOARD, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        body = None
        while time.time() < deadline and body is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz?token=must-not-be-logged", timeout=2) as r:
                    assert r.status == 200
                    body = r.read()
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
        assert body is not None, "gunicorn never served /healthz"
        assert b'"service":"dashboard"' in body.replace(b" ", b"")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    log = out.decode("utf-8", "replace")
    assert proc.returncode == 0, log
    assert "development server" not in log
    assert "GET /healthz" in log
    assert "must-not-be-logged" not in log
