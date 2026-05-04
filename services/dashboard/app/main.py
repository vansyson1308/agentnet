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
        return redirect(referer or url_for('index'))
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
    agents = []
    error = None
    try:
        # Fetch public agents (no auth required for listing)
        agents = api_client.fetch_agents(limit=50)
    except APIError as e:
        error = e.message
        app.logger.warning(f"Failed to fetch agents for metaverse: {e}")
    except Exception as e:
        error = "Could not load agents at this time."
        app.logger.error(f"Unexpected error fetching agents: {e}")
    # If user is logged in, we could also fetch their own agents, but for now show public list
    return render_template("metaverse.html", agents=agents, error=error)


@app.route("/marketplace")
def marketplace_page():
    """Agent marketplace – public listing."""
    agents = []
    error = None
    try:
        agents = api_client.fetch_agents(limit=100)
    except APIError as e:
        error = e.message
        app.logger.warning(f"Failed to fetch agents for marketplace: {e}")
    except Exception as e:
        error = "Could not load marketplace at this time."
        app.logger.error(f"Unexpected error fetching agents: {e}")
    return render_template("marketplace.html", agents=agents, error=error)


# ============================================================
# AUTH ROUTES (session-based login)
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Login form and handler."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("login.html")
        try:
            data = api_client.login(username, password)
            session["access_token"] = data["access_token"]
            session["refresh_token"] = data.get("refresh_token", "")
            session["user"] = data.get("user", username)
            flash("Login successful!", "success")
            next_page = request.args.get("next") or url_for("metaverse_page")
            return redirect(next_page)
        except APIError as e:
            flash(f"Login failed: {e.message}", "danger")
        except Exception as e:
            flash("An unexpected error occurred during login.", "danger")
            app.logger.error(f"Login error: {e}")
    return render_template("login.html")


@app.route("/logout")
def logout_page():
    """Clear session and redirect to landing."""
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("landing_page"))


# ============================================================
# PROTECTED ROUTES (require authentication)
# ============================================================

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "access_token" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/wallet")
@login_required
def wallet_page():
    """User wallet and token management."""
    try:
        wallet = api_client.get_wallet()
    except APIError as e:
        flash(f"Could not load wallet: {e.message}", "danger")
        wallet = None
    except Exception as e:
        flash("Error loading wallet.", "danger")
        app.logger.error(f"Wallet error: {e}")
        wallet = None
    return render_template("wallet.html", wallet=wallet)


@app.route("/tasks")
@login_required
def tasks_page():
    """User task management."""
    tasks = []
    error = None
    try:
        tasks = api_client.get_tasks()
    except APIError as e:
        error = e.message
        app.logger.warning(f"Failed to fetch tasks: {e}")
    except Exception as e:
        error = "Could not load tasks."
        app.logger.error(f"Tasks error: {e}")
    return render_template("tasks.html", tasks=tasks, error=error)


@app.route("/collaboration")
@login_required
def collaboration_page():
    """Multi-agent collaboration chat."""
    return render_template("collaboration.html")


@app.route("/notifications")
@login_required
def notifications_page():
    """User notifications."""
    notifications = []
    try:
        notifications = api_client.get_notifications()
    except Exception as e:
        app.logger.warning(f"Failed to fetch notifications: {e}")
    return render_template("notifications.html", notifications=notifications)


@app.route("/directory")
@login_required
def directory_page():
    """Agent directory – browse all registered agents."""
    agents = []
    error = None
    try:
        agents = api_client.fetch_agents(limit=200)
    except APIError as e:
        error = e.message
    except Exception as e:
        error = "Could not load directory."
        app.logger.error(f"Directory error: {e}")
    return render_template("directory.html", agents=agents, error=error)


@app.route("/agents/new", methods=["GET", "POST"])
@login_required
def register_agent_page():
    """Register a new agent."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        endpoint = request.form.get("endpoint", "").strip()
        capabilities = request.form.get("capabilities", "").strip()
        if not name:
            flash("Agent name is required.", "danger")
            return render_template("new_agent.html")
        caps_list = [c.strip() for c in capabilities.split(",") if c.strip()]
        try:
            agent = api_client.create_agent(
                name=name,
                description=description,
                endpoint=endpoint,
                capabilities=caps_list
            )
            flash(f"Agent '{name}' registered successfully!", "success")
            return redirect(url_for("directory_page"))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
        except Exception as e:
            flash("Unexpected error during registration.", "danger")
            app.logger.error(f"Agent registration error: {e}")
    return render_template("new_agent.html")


@app.route("/agents/<agent_id>")
def agent_detail_page(agent_id):
    """Agent details page – public information."""
    agent = None
    error = None
    try:
        agent = api_client.get_agent(agent_id)
    except APIError as e:
        error = e.message
        app.logger.warning(f"Failed to fetch agent {agent_id}: {e}")
    except Exception as e:
        error = "Could not load agent details."
        app.logger.error(f"Agent detail error for {agent_id}: {e}")
    return render_template("agent_detail.html", agent=agent, error=error)


@app.route("/agents/<agent_id>/offers/create", methods=["GET", "POST"])
@login_required
def create_offer_page(agent_id):
    """Create an offer to hire an agent."""
    agent = None
    error = None
    try:
        agent = api_client.get_agent(agent_id)
    except APIError as e:
        error = e.message
    except Exception as e:
        error = "Could not load agent."
        app.logger.error(f"Offer create agent fetch error: {e}")
    if request.method == "POST":
        task = request.form.get("task", "").strip()
        budget = request.form.get("budget", "0")
        deadline = request.form.get("deadline", "")
        if not task:
            flash("Task description is required.", "danger")
            return render_template("create_offer.html", agent=agent)
        try:
            offer = api_client.create_offer(agent_id, task=task, budget=float(budget), deadline=deadline or None)
            flash("Offer created successfully!", "success")
            return redirect(url_for("agent_detail_page", agent_id=agent_id))
        except APIError as e:
            flash(f"Offer creation failed: {e.message}", "danger")
        except Exception as e:
            flash("Unexpected error creating offer.", "danger")
            app.logger.error(f"Offer creation error: {e}")
    return render_template("create_offer.html", agent=agent)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    debug = _IS_DEV
    app.run(host="0.0.0.0", port=port, debug=debug)