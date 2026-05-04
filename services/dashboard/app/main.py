import os
import json
import uuid
import time
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
from .api_client import api_client, APIError, AuthRequiredError
import pathlib
import typing as _typing

_ENV = os.getenv("ENVIRONMENT", "development").lower()
_IS_DEV = _ENV == "development"


def _resolve_flask_secret() -> str:
    """FLASK_SECRET_KEY is required in non-dev. In dev, fall back to a
    per-process random key (sessions reset on restart — fine locally)."""
    val = os.getenv("FLASK_SECRET_KEY", "")
    if val:
        return val
    if _IS_DEV:
        return "dev_secret_key_" + str(uuid.uuid4())
    raise RuntimeError(
        "FLASK_SECRET_KEY is required in non-development environments. "
        "Generate via `openssl rand -hex 32`."
    )


app = Flask(__name__)
app.secret_key = _resolve_flask_secret()

# Behind Caddy / nginx the X-Forwarded-* headers tell us the real scheme
# and host. Without ProxyFix, url_for() generates http:// links and
# session cookies' Secure flag would be wrong.
if os.getenv("BEHIND_PROXY", "").lower() == "true":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Cookie hardening — Secure cookies require HTTPS, so only set in non-dev.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not _IS_DEV,
)


# --- Liveness / readiness probes for orchestrators ---


@app.route("/healthz")
def healthz():
    """Liveness probe — answer 200 immediately, no upstream calls."""
    return jsonify({"status": "ok", "service": "dashboard"}), 200


@app.route("/readyz")
def readyz():
    """Readiness — best-effort registry ping. 503 if it can't reach the
    backend so traffic isn't routed in until the upstream is healthy."""
    try:
        # Don't fail readiness if registry is just slow; 2s timeout.
        ok = api_client.health_registry(timeout=2.0)
    except Exception as e:
        return jsonify({"status": "not_ready", "error": str(e)}), 503
    if not ok:
        return jsonify({"status": "not_ready"}), 503
    return jsonify({"status": "ready"}), 200


@app.errorhandler(AuthRequiredError)
def handle_auth_required(e):
    flash("Please log in to continue.", "warning")
    return redirect(url_for('login_page'))


def derive_trust_context(agent):
    total = agent.get('total_tasks_completed', 0) + agent.get('total_tasks_failed', 0) + agent.get('total_tasks_timeout', 0)
    success = agent.get('success_rate', 0.0)
    timeouts = agent.get('total_tasks_timeout', 0)
    tier = str(agent.get('reputation_tier', 'unranked')).capitalize()
    
    label = "Unknown Reliability"
    color = "#64748b"
    if total < 5:
        label = "Limited History"
        color = "#8b5cf6"
    elif success >= 0.90 and timeouts == 0:
        label = "Highly Reliable"
        color = "#10b981"
    elif success >= 0.80:
        label = "Generally Reliable"
        color = "#3b82f6"
    elif timeouts > (total * 0.1):
        label = "Frequent Timeout Risk"
        color = "#ef4444"
    elif timeouts > 0:
        label = "Occasional Timeout Risk"
        color = "#f59e0b"
    else:
        label = "Moderate Reliability"
        color = "#64748b"
        
    return {
        "total": total,
        "label": label,
        "color": color,
        "tier": tier,
        "success_percent": f"{success * 100:.1f}%",
        "timeouts": timeouts
    }


app.jinja_env.filters['trust_context'] = derive_trust_context


@app.errorhandler(404)
def handle_not_found(e):
    app.logger.warning(f"404: {request.path}")
    if request.path.startswith("/werewolf") or request.path.startswith("/api"):
        return jsonify({"error": "not_found", "path": request.path}), 404
    # Don't flash for static assets or crawler requests
    if request.path in ("/favicon.ico", "/robots.txt") or \
       request.path.startswith("/static/") or \
       request.path.startswith("/assets/"):
        return "", 204
    flash("Page not found.", "warning")
    return redirect(url_for('landing_page'))


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, APIError):
        flash(f"API Error: {e.message}", "danger")
        referer = request.headers.get("Referer")
        return redirect(referer or url_for('metaverse_page'))
    from werkzeug.exceptions import NotFound
    if isinstance(e, NotFound):
        return handle_not_found(e)
    app.logger.error(f"Unhandled Exception: {e}")
    if request.path in ("/favicon.ico", "/robots.txt"):
        return "", 204
    if request.path.startswith("/api"):
        return jsonify({"error": "internal_error"}), 500
    flash("An unexpected backend error occurred.", "danger")
    return render_template("error.html", error=str(e) if app.debug else "Internal Server Error"), 500


@app.context_processor
def inject_user():
    return dict(is_logged_in="access_token" in session)


# ============================================================
# PUBLIC ROUTES (no auth required)
# ============================================================

@app.route("/landing")
def landing_page():
    return render_template("landing.html")


@app.route("/")
def index():
    return redirect(url_for('metaverse_page'))


@app.route("/metaverse")
def metaverse_page():
    """Command Center – main dashboard. Publicly accessible; data shown only if authenticated."""
    try:
        agents = api_client.fetch_agents(limit=100)
    except Exception:
        agents = []
    
    # Compute summary statistics if authenticated (otherwise show placeholder)
    total_agents = len(agents)
    total_tasks = sum(a.get('total_tasks_completed', 0) + a.get('total_tasks_failed', 0) + a.get('total_tasks_timeout', 0) for a in agents)
    online_count = sum(1 for a in agents if a.get('status') == 'online')
    high_reliability_count = sum(1 for a in agents if derive_trust_context(a)['label'] in ('Highly Reliable', 'Generally Reliable'))
    
    # Only expose full agent list if user is logged in
    is_logged_in = bool(session.get('access_token'))
    if not is_logged_in:
        agents = []
    
    return render_template(
        "metaverse.html",
        agents=agents,
        is_logged_in=is_logged_in,
        total_agents=total_agents,
        total_tasks=total_tasks,
        online_count=online_count,
        high_reliability_count=high_reliability_count
    )


# ... remaining routes follow the same pattern (login, register, marketplace, etc.) ...
# ============================================================
# AUTH ROUTES
# ============================================================

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not email or not password:
        flash("Email and password are required.", "danger")
        return render_template("login.html")
    try:
        resp = api_client.login(email, password)
        session["access_token"] = resp["access_token"]
        session["user_email"] = email
        flash("Welcome back, commander!", "success")
        return redirect(url_for('metaverse_page'))
    except APIError as e:
        flash(f"Login failed: {e.message}", "danger")
        return render_template("login.html"), 401


@app.route("/register")
def register_page():
    return render_template("register.html")


@app.route("/register", methods=["POST"])
def register_post():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not email or not password:
        flash("Email and password are required.", "danger")
        return render_template("register.html")
    try:
        resp = api_client.register(email, password)
        session["access_token"] = resp["access_token"]
        session["user_email"] = email
        flash("Account created. Welcome to J.A.R.V.I.S.!", "success")
        return redirect(url_for('metaverse_page'))
    except APIError as e:
        flash(f"Registration failed: {e.message}", "danger")
        return render_template("register.html"), 400


@app.route("/logout")
def logout_page():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for('landing_page'))


# ============================================================
# PROTECTED ROUTES (require auth)
# ============================================================

def require_auth():
    if "access_token" not in session:
        raise AuthRequiredError()


@app.route("/marketplace")
def marketplace_page():
    try:
        agents = api_client.fetch_agents(limit=100)
    except Exception:
        agents = []
    return render_template("marketplace.html", agents=agents)


@app.route("/directory")
def directory_page():
    require_auth()
    try:
        agents = api_client.fetch_agents(limit=200)
    except Exception:
        agents = []
    return render_template("directory.html", agents=agents)


@app.route("/wallet")
def wallet_page():
    require_auth()
    return render_template("wallet.html")


@app.route("/tasks")
def tasks_page():
    require_auth()
    return render_template("tasks.html")


@app.route("/collaboration")
def collaboration_page():
    require_auth()
    return render_template("collaboration.html")


@app.route("/notifications")
def notifications_page():
    require_auth()
    return render_template("notifications.html")


@app.route("/new-agent")
def new_agent_page():
    require_auth()
    return render_template("new_agent.html")


@app.route("/new-agent", methods=["POST"])
def register_agent_page():
    require_auth()
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    endpoint = request.form.get("endpoint", "").strip()
    capabilities = request.form.get("capabilities", "").strip()
    if not name:
        flash("Agent name is required.", "danger")
        return redirect(url_for('new_agent_page'))
    try:
        resp = api_client.register_agent(name, description, endpoint, capabilities)
        flash(f"Agent '{name}' registered successfully!", "success")
        return redirect(url_for('directory_page'))
    except APIError as e:
        flash(f"Failed to register agent: {e.message}", "danger")
        return redirect(url_for('new_agent_page'))


# ============================================================
# API-ONLY ROUTES (for AJAX / external use)
# ============================================================

@app.route("/api/agents")
def api_agents():
    try:
        agents = api_client.fetch_agents(limit=100)
        return jsonify(agents)
    except APIError as e:
        return jsonify({"error": e.message}), e.status_code


@app.route("/api/agents/<agent_id>")
def api_agent(agent_id):
    try:
        agent = api_client.fetch_agent(agent_id)
        return jsonify(agent)
    except APIError as e:
        return jsonify({"error": e.message}), e.status_code