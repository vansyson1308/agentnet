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
    user_balance = None
    recent_tasks = []
    wallet_address = None

    if "access_token" in session:
        try:
            agents = api_client.fetch_agents(limit=50)
            # Try to augment with trust context (already done in template via filter)
            # Fetch wallet info if available
            try:
                wallet = api_client.get_wallet()
                user_balance = wallet.get("balance", 0)
                wallet_address = wallet.get("address")
            except Exception:
                pass

            # Fetch recent tasks
            try:
                tasks = api_client.fetch_tasks(limit=10)
                recent_tasks = tasks if isinstance(tasks, list) else []
            except Exception:
                pass
        except APIError as e:
            flash(f"Could not load data: {e.message}", "danger")
        except Exception as e:
            app.logger.error(f"metaverse_page error: {e}")
            flash("An error occurred while loading the dashboard.", "danger")

    return render_template(
        "metaverse.html",
        agents=agents,
        user_balance=user_balance,
        recent_tasks=recent_tasks,
        wallet_address=wallet_address,
        is_logged_in="access_token" in session,
    )


# ============================================================
# AUTHENTICATED ROUTES
# ============================================================

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    email = request.form.get("email")
    password = request.form.get("password")
    if not email or not password:
        flash("Email and password are required.", "danger")
        return redirect(url_for('login_page'))
    try:
        result = api_client.login(email, password)
        session["access_token"] = result["access_token"]
        session["refresh_token"] = result.get("refresh_token")
        flash("Welcome back, Commander.", "success")
        return redirect(url_for('metaverse_page'))
    except APIError as e:
        flash(f"Login failed: {e.message}", "danger")
        return redirect(url_for('login_page'))


@app.route("/logout")
def logout_page():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for('metaverse_page'))


@app.route("/register")
def register_page():
    return render_template("register.html")


@app.route("/register", methods=["POST"])
def register_post():
    email = request.form.get("email")
    password = request.form.get("password")
    username = request.form.get("username")
    if not email or not password:
        flash("Email and password are required.", "danger")
        return redirect(url_for('register_page'))
    try:
        api_client.register(email, password, username)
        flash("Registration successful. Please sign in.", "success")
        return redirect(url_for('login_page'))
    except APIError as e:
        flash(f"Registration failed: {e.message}", "danger")
        return redirect(url_for('register_page'))


@app.route("/marketplace")
def marketplace_page():
    agents = []
    try:
        agents = api_client.fetch_agents(limit=100)
    except APIError as e:
        flash(f"Could not load marketplace: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load marketplace.", "danger")
    return render_template("marketplace.html", agents=agents)


@app.route("/directory")
def directory_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    agents = []
    try:
        agents = api_client.fetch_agents(limit=200)
    except APIError as e:
        flash(f"Could not load directory: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load directory.", "danger")
    return render_template("directory.html", agents=agents)


@app.route("/wallet")
def wallet_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    wallet = {}
    transactions = []
    try:
        wallet = api_client.get_wallet()
        transactions = api_client.get_wallet_transactions(limit=20)
    except APIError as e:
        flash(f"Could not load wallet: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load wallet.", "danger")
    return render_template("wallet.html", wallet=wallet, transactions=transactions)


@app.route("/tasks")
def tasks_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    tasks = []
    try:
        tasks = api_client.fetch_tasks(limit=50)
    except APIError as e:
        flash(f"Could not load tasks: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load tasks.", "danger")
    return render_template("tasks.html", tasks=tasks)


@app.route("/notifications")
def notifications_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    notifications = []
    try:
        notifications = api_client.fetch_notifications(limit=50)
    except APIError as e:
        flash(f"Could not load notifications: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load notifications.", "danger")
    return render_template("notifications.html", notifications=notifications)


@app.route("/my-agents")
def my_agents_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    agents = []
    try:
        agents = api_client.fetch_my_agents()
    except APIError as e:
        flash(f"Could not load your agents: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load your agents.", "danger")
    return render_template("my_agents.html", agents=agents)


@app.route("/my-agents/new", methods=["GET"])
def new_agent_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    return render_template("new_agent.html")


@app.route("/my-agents/new", methods=["POST"])
def register_agent_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    name = request.form.get("name")
    description = request.form.get("description")
    endpoint = request.form.get("endpoint")
    capabilities = request.form.get("capabilities")
    if not name:
        flash("Agent name is required.", "danger")
        return redirect(url_for('new_agent_page'))
    try:
        agent = api_client.register_agent(
            name=name,
            description=description,
            endpoint=endpoint,
            capabilities=[c.strip() for c in capabilities.split(",") if c.strip()] if capabilities else [],
        )
        flash(f"Agent '{agent.get('name', name)}' registered successfully.", "success")
        return redirect(url_for('my_agents_page'))
    except APIError as e:
        flash(f"Registration failed: {e.message}", "danger")
        return redirect(url_for('new_agent_page'))


@app.route("/collaboration")
def collaboration_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    conversations = []
    try:
        conversations = api_client.fetch_conversations(limit=20)
    except APIError as e:
        flash(f"Could not load conversations: {e.message}", "danger")
    except Exception as e:
        flash("Failed to load conversations.", "danger")
    return render_template("collaboration.html", conversations=conversations)


@app.route("/api/agents")
def api_agents():
    """Public API endpoint to get agent list (used by frontend scripts)."""
    agents = []
    try:
        agents = api_client.fetch_agents(limit=100)
    except Exception:
        pass
    return jsonify(agents)


@app.route("/create-offer", methods=["GET"])
def create_offer_page():
    if "access_token" not in session:
        return handle_auth_required(None)
    agents = []
    try:
        agents = api_client.fetch_my_agents()
    except Exception:
        pass
    return render_template("create_offer.html", agents=agents)


@app.route("/create-offer", methods=["POST"])
def create_offer_post():
    if "access_token" not in session:
        return handle_auth_required(None)
    agent_id = request.form.get("agent_id")
    title = request.form.get("title")
    description = request.form.get("description")
    price = request.form.get("price")
    if not agent_id or not title or not price:
        flash("Agent, title, and price are required.", "danger")
        return redirect(url_for('create_offer_page'))
    try:
        offer = api_client.create_offer(
            agent_id=agent_id,
            title=title,
            description=description,
            price=float(price),
        )
        flash("Offer created successfully.", "success")
        return redirect(url_for('my_agents_page'))
    except APIError as e:
        flash(f"Offer creation failed: {e.message}", "danger")
        return redirect(url_for('create_offer_page'))


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=_IS_DEV)