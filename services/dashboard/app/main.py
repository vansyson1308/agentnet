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
    tasks = []
    try:
        if "access_token" in session:
            agents = api_client.get_agents(limit=50)
            tasks = api_client.get_tasks()
    except (APIError, AuthRequiredError) as e:
        flash("Unable to load dashboard data. Please log in if you haven't.", "info")
    return render_template("metaverse.html", agents=agents, tasks=tasks)

@app.route("/marketplace")
def marketplace():
    """Public marketplace listing agents."""
    search = request.args.get("search", "")
    category = request.args.get("category", "")
    sort = request.args.get("sort", "")
    order = request.args.get("order", "")
    agents = []
    try:
        agents = api_client.fetch_agents(search=search, category=category, sort=sort, order=order)
    except (APIError, AuthRequiredError) as e:
        flash("Could not load marketplace. Please try again later.", "warning")
    return render_template("marketplace.html", agents=agents, search=search, category=category, sort=sort, order=order)

@app.route("/directory")
def directory_page():
    """Authenticated users only: list all agents on the network."""
    agents = []
    try:
        agents = api_client.get_agents(limit=200)
    except AuthRequiredError:
        flash("Please sign in to view the agent directory.", "warning")
        return redirect(url_for('login_page'))
    except APIError as e:
        flash(f"API error: {e.message}", "danger")
    return render_template("directory.html", agents=agents)

@app.route("/wallet")
def wallet_page():
    """Show user wallets and transactions."""
    wallets = []
    transactions = []
    try:
        wallets = api_client.get_wallets()
        transactions = api_client.get_transactions()
    except AuthRequiredError:
        flash("Please sign in to access your wallet.", "warning")
        return redirect(url_for('login_page'))
    except APIError as e:
        flash(f"API error: {e.message}", "danger")
    return render_template("wallet.html", wallets=wallets, transactions=transactions)

@app.route("/tasks")
def tasks_page():
    """List all tasks for the logged-in user."""
    tasks = []
    try:
        tasks = api_client.get_tasks()
    except AuthRequiredError:
        flash("Please sign in to view tasks.", "warning")
        return redirect(url_for('login_page'))
    except APIError as e:
        flash(f"API error: {e.message}", "danger")
    return render_template("tasks.html", tasks=tasks)

@app.route("/notifications")
def notifications_page():
    """Placeholder for notifications."""
    return render_template("notifications.html")

@app.route("/collaboration")
def collaboration_page():
    """Placeholder for collaboration/chat."""
    return render_template("collaboration.html")

@app.route("/agents/new", methods=["GET", "POST"])
def register_agent_page():
    """Register a new agent."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        endpoint = request.form.get("endpoint", "").strip()
        capabilities = [c.strip() for c in request.form.get("capabilities", "").split(",") if c.strip()]
        if not name:
            flash("Agent name is required.", "danger")
            return render_template("new_agent.html")
        data = {
            "name": name,
            "description": description,
            "endpoint": endpoint,
            "capabilities": capabilities
        }
        try:
            agent = api_client.create_agent(data)
            flash(f"Agent '{agent.get('name', name)}' registered successfully!", "success")
            return redirect(url_for('directory_page'))
        except AuthRequiredError:
            flash("Please sign in first.", "warning")
            return redirect(url_for('login_page'))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
    return render_template("new_agent.html")

# ============================================================
# AUTH ROUTES
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("login.html")
        try:
            token_data = api_client.login(username, password)
            if "access_token" in token_data:
                session["access_token"] = token_data["access_token"]
                flash("Welcome back, Commander.", "success")
                return redirect(url_for('metaverse_page'))
            else:
                flash("Invalid response from authentication server.", "danger")
        except APIError as e:
            flash(f"Login failed: {e.message}", "danger")
        except Exception as e:
            flash("Unexpected error during login.", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("register.html")
        try:
            result = api_client.register(email, password)
            flash("Account created! You can now sign in.", "success")
            return redirect(url_for('login_page'))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
        except Exception as e:
            flash("Unexpected error during registration.", "danger")
    return render_template("register.html")

@app.route("/logout")
def logout_page():
    session.pop("access_token", None)
    flash("You have been signed out.", "info")
    return redirect(url_for('landing_page'))

# ============================================================
# WALLET FUNDING (POST only, from a form)
# ============================================================

@app.route("/wallet/fund", methods=["POST"])
def fund_wallet():
    wallet_id = request.form.get("wallet_id", "").strip()
    amount_str = request.form.get("amount", "").strip()
    try:
        amount = float(amount_str)
    except (ValueError, TypeError):
        flash("Invalid amount.", "danger")
        return redirect(url_for('wallet_page'))
    if not wallet_id:
        flash("Wallet ID is required.", "danger")
        return redirect(url_for('wallet_page'))
    try:
        result = api_client.fund_wallet(wallet_id, amount)
        flash(f"Wallet funded with {amount} credits.", "success")
    except AuthRequiredError:
        flash("Please sign in first.", "warning")
        return redirect(url_for('login_page'))
    except APIError as e:
        flash(f"Funding failed: {e.message}", "danger")
    return redirect(url_for('wallet_page'))