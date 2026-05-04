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

if os.getenv("BEHIND_PROXY", "").lower() == "true":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not _IS_DEV,
)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "dashboard"}), 200


@app.route("/readyz")
def readyz():
    try:
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
        agents = api_client.fetch_agents(limit=50, sort="success_rate", order="desc")
        enriched = []
        for a in agents:
            ctx = derive_trust_context(a)
            enriched.append({**a, "trust": ctx})
        return render_template("metaverse.html", agents=enriched, is_logged_in=bool(session.get("access_token")))
    except APIError as e:
        app.logger.error(f"Failed to fetch agents for metaverse: {e}")
        flash("Could not load the agent directory. The registry might be unavailable.", "warning")
        return render_template("metaverse.html", agents=[], is_logged_in=bool(session.get("access_token")))
    except Exception as e:
        app.logger.error(f"Metaverse error: {e}")
        flash("An error occurred while loading the command center.", "danger")
        return render_template("metaverse.html", agents=[], is_logged_in=bool(session.get("access_token")))


@app.route("/marketplace")
def marketplace_page():
    """Marketplace – browse and search agents. Always accessible."""
    try:
        search = request.args.get("search", "")
        category = request.args.get("category", "")
        sort = request.args.get("sort", "updated_at")
        order = request.args.get("order", "desc")
        agents = api_client.fetch_agents(search=search, category=category, sort=sort, order=order, limit=100)
        enriched = []
        for a in agents:
            ctx = derive_trust_context(a)
            enriched.append({**a, "trust": ctx})
        return render_template("marketplace.html", agents=enriched, is_logged_in=bool(session.get("access_token")))
    except Exception as e:
        app.logger.error(f"Marketplace error: {e}")
        flash("Could not load the marketplace. Please try again later.", "warning")
        return render_template("marketplace.html", agents=[], is_logged_in=bool(session.get("access_token")))


# ============================================================
# AUTHENTICATION ROUTES
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("login.html")
        try:
            data = api_client.login(email=email, password=password)
            session["access_token"] = data["access_token"]
            session["refresh_token"] = data.get("refresh_token", "")
            session["user"] = data.get("user", {})
            flash("Welcome back, Commander!", "success")
            return redirect(url_for("metaverse_page"))
        except AuthRequiredError:
            flash("Invalid email or password.", "danger")
        except APIError as e:
            flash(f"Login failed: {e.message}", "danger")
        except Exception as e:
            flash("An unexpected error occurred during login.", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        name = request.form.get("name", "")
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("register.html")
        try:
            data = api_client.register(email=email, password=password, name=name)
            flash("Account created successfully. Please log in.", "success")
            return redirect(url_for("login_page"))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
        except Exception as e:
            flash("An unexpected error occurred during registration.", "danger")
    return render_template("register.html")


@app.route("/logout")
def logout_page():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("landing_page"))


# ============================================================
# AUTH-PROTECTED ROUTES (require token)
# ============================================================

def login_required(f):
    """Decorator to require authentication."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "access_token" not in session:
            flash("Please sign in to access this page.", "warning")
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/directory")
@login_required
def directory_page():
    """Directory of registered agents (for authenticated users)."""
    try:
        agents = api_client.fetch_agents(limit=100, sort="created_at", order="desc")
        enriched = []
        for a in agents:
            ctx = derive_trust_context(a)
            enriched.append({**a, "trust": ctx})
        return render_template("directory.html", agents=enriched)
    except APIError as e:
        flash(f"Failed to load directory: {e.message}", "danger")
        return render_template("directory.html", agents=[])


@app.route("/wallet")
@login_required
def wallet_page():
    """Wallet overview – balance and transaction history."""
    try:
        wallet = api_client.get_wallet()
        transactions = api_client.get_transactions()
        return render_template("wallet.html", wallet=wallet, transactions=transactions)
    except APIError as e:
        flash(f"Failed to load wallet: {e.message}", "danger")
        return render_template("wallet.html", wallet={}, transactions=[])


@app.route("/tasks")
@login_required
def tasks_page():
    """Task management – review and assign tasks."""
    try:
        tasks = api_client.fetch_tasks()
        return render_template("tasks.html", tasks=tasks)
    except APIError as e:
        flash(f"Failed to load tasks: {e.message}", "danger")
        return render_template("tasks.html", tasks=[])


@app.route("/collaboration")
@login_required
def collaboration_page():
    """Collaboration – chat with agents."""
    try:
        conversations = api_client.get_conversations()
        return render_template("collaboration.html", conversations=conversations)
    except APIError as e:
        flash(f"Failed to load conversations: {e.message}", "danger")
        return render_template("collaboration.html", conversations=[])


@app.route("/notifications")
@login_required
def notifications_page():
    """Notifications – agent activity and system alerts."""
    try:
        notifs = api_client.get_notifications()
        return render_template("notifications.html", notifications=notifs)
    except APIError as e:
        flash(f"Failed to load notifications: {e.message}", "danger")
        return render_template("notifications.html", notifications=[])


@app.route("/demo_stream")
def demo_stream():
    """SSE endpoint for demonstration (not used in production)."""
    def generate():
        for i in range(5):
            yield f"data: {json.dumps({'count': i, 'ts': time.time()})}\n\n"
            time.sleep(0.5)
    return Response(stream_with_context(generate()), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=_IS_DEV)