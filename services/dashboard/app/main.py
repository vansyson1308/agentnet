import os
import json
import uuid
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
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
    except Exception:
        # Never echo the exception: it names the private registry URL.
        app.logger.warning("readyz: registry probe raised", exc_info=True)
        ok = False
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
    
    if total < 5:
        label, color = "Limited History", "#8b5cf6"
    elif timeouts > (total * 0.1):
        label, color = "Frequent Timeout Risk", "#ef4444"
    elif success >= 0.90 and timeouts == 0:
        label, color = "Highly Reliable", "#10b981"
    elif timeouts > 0:
        label, color = "Occasional Timeout Risk", "#f59e0b"
    elif success >= 0.80:
        label, color = "Generally Reliable", "#3b82f6"
    else:
        label, color = "Moderate Reliability", "#64748b"

    return {
        "total": total,
        "label": label,
        "color": color,
        "tier": tier,
        "success_percent": f"{success * 100:.1f}%",
        "timeouts": timeouts
    }


app.jinja_env.filters['trust_context'] = derive_trust_context


def capability_name(cap) -> str:
    """A capability as the text a badge shows: the registry returns objects
    ({"name", "description", "price"}); older shapes are plain strings."""
    if isinstance(cap, dict):
        return str(cap.get("name") or cap.get("description") or "capability")
    return str(cap)


app.jinja_env.filters['capability_name'] = capability_name


@app.errorhandler(404)
def handle_not_found(e):
    app.logger.warning(f"404: {request.path}")
    if request.path.startswith("/werewolf") or request.path.startswith("/api"):
        return jsonify({"error": "not_found", "path": request.path}), 404
    if request.path in ("/favicon.ico", "/robots.txt") or \
       request.path.startswith("/static/") or \
       request.path.startswith("/assets/"):
        return "", 204
    # A missing page answers 404; a redirect to /landing would disguise it.
    return render_template("error.html", title="Page Not Found", error="That page does not exist."), 404


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
    return render_template("error.html", title="Internal Server Error", error=str(e) if app.debug else "Internal Server Error"), 500


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
    """Marketplace – browse and search agents. Always accessible: when the
    registry is unreachable the page still renders, with an empty state."""
    search = request.args.get("search", "")
    category = request.args.get("category", "")
    sort = request.args.get("sort", "")
    order = request.args.get("order", "desc")
    agents, unavailable = [], False
    try:
        fetched = api_client.fetch_agents(search=search, category=category, sort=sort or None, order=order, limit=100)
        agents = [{**a, "trust": derive_trust_context(a)} for a in fetched]
    except Exception as e:
        app.logger.warning("marketplace: registry unavailable (%s)", type(e).__name__)
        unavailable = True
    return render_template(
        "marketplace.html", agents=agents, registry_unavailable=unavailable,
        current_search=search, current_category=category, current_sort=sort, current_order=order,
    )


# ============================================================
# AUTHENTICATION ROUTES
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("login.html"), 400
        try:
            data = api_client.login(email=email, password=password)
        except APIError as e:
            if e.status_code == 401:
                flash("Invalid email or password.", "danger")
            elif e.status_code == 403:
                flash("Please verify your email address before signing in.", "warning")
            else:
                app.logger.warning("login: registry error (%s)", e.status_code)
                flash("Sign-in is unavailable right now. Please try again later.", "danger")
            return render_template("login.html"), 400 if e.status_code in (401, 403) else 503
        session.clear()
        session["access_token"] = data["access_token"]
        flash("Welcome back, Commander!", "success")
        return redirect(url_for("metaverse_page"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password are required.", "danger")
            return render_template("register.html"), 400
        try:
            api_client.register(email=email, password=password)
        except APIError as e:
            if e.status_code == 400 and e.detail:
                flash(f"Registration failed: {e.detail}", "danger")
            else:
                app.logger.warning("register: registry error (%s)", e.status_code)
                flash("Registration is unavailable right now. Please try again later.", "danger")
            return render_template("register.html"), 400 if e.status_code == 400 else 503
        flash("Account created. Check your email for the verification link, then sign in.", "success")
        return redirect(url_for("login_page"))
    return render_template("register.html")


@app.route("/logout")
def logout_page():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("landing_page"))


# ============================================================
# A2A NETWORK (Phase 8, ADR-0009) -- public, read-only
# ============================================================

_UUID_RE = __import__("re").compile(r"^[0-9a-fA-F-]{36}$")
ECONOMICS_EXTENSION_URI = "https://agentnet.io.vn/a2a/extensions/economics/v1"


@app.route("/network")
def network_page():
    """The A2A network: status, endpoints, how to connect, marketplace agents
    reachable over A2A and a STRUCTURAL federation summary. Everything shown
    comes from public registry surfaces; nothing is invented when a feature
    is off -- the page says it is off."""
    card = conformance = federation = None
    agents = []
    try:
        card = api_client.fetch_a2a_network_card()
        conformance = api_client.fetch_a2a_conformance() if card else None
        federation = api_client.fetch_federation_summary() if card else None
        agents = api_client.fetch_agents(limit=24, sort="success_rate", order="desc") if card else []
    except APIError as e:
        app.logger.warning("network page: registry unavailable (%s)", e.status_code)
        flash("The registry is unavailable right now.", "warning")
    return render_template("network.html", card=card, conformance=conformance, federation=federation, agents=agents)


@app.route("/network/agents/<agent_id>")
def network_agent_page(agent_id):
    """One marketplace agent's A2A card: the gateway interfaces with its
    tenant, its skills with prices, and copy-paste calls."""
    if not _UUID_RE.match(agent_id or ""):
        return redirect(url_for("network_page"))
    card = api_client.fetch_agent_a2a_card(agent_id)
    if card is None:
        flash("That agent is not reachable over A2A.", "warning")
        return redirect(url_for("network_page"))
    econ = next((e for e in card.get("capabilities", {}).get("extensions", []) if e.get("uri") == ECONOMICS_EXTENSION_URI), {})
    prices = {k: int(v) for k, v in (econ.get("params", {}).get("skillPrices") or {}).items()}
    interfaces = {i.get("protocolBinding"): i for i in card.get("supportedInterfaces", [])}
    return render_template("network_agent.html", agent_id=agent_id, card=card, prices=prices, interfaces=interfaces, econ_uri=ECONOMICS_EXTENSION_URI)
