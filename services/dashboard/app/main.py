import os
import json
import uuid
import time
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, stream_with_context
from .api_client import api_client, APIError, AuthRequiredError
import pathlib
import typing as _typing

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_key_" + str(uuid.uuid4()))

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

@app.route("/marketplace")
def marketplace_page():
    search = request.args.get("search")
    category = request.args.get("category")
    sort = request.args.get("sort", "rating")
    order = request.args.get("order", "desc")
    try:
        agents = api_client.fetch_agents(search=search, category=category, sort=sort, order=order)
    except APIError as e:
        flash(f"Could not load marketplace: {e.message}", "danger")
        agents = []
    return render_template("marketplace.html", agents=agents)

@app.route("/metaverse")
def metaverse_page():
    return render_template("metaverse.html")

# ============================================================
# AUTH ROUTES
# ============================================================

@app.route("/")
def index():
    if "access_token" not in session:
        return redirect(url_for('landing_page'))
    
    try:
        wallets = api_client.get_wallets()
        total_credits = sum(w.get("balance_credits", 0) for w in wallets)
        total_usdc = sum(w.get("balance_usdc", 0) for w in wallets)
    except APIError:
        wallets = []
        total_credits = 0
        total_usdc = 0
        flash("Could not load wallet balances.", "warning")
        
    return render_template("index.html", wallets=wallets, total_credits=total_credits, total_usdc=total_usdc)

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        if not email or not password:
            flash("Email and password required", "danger")
            return render_template("login.html")
            
        try:
            resp = api_client.login(email, password)
            session["access_token"] = resp.get("access_token")
            flash("Logged in successfully.", "success")
            return redirect(url_for('index'))
        except APIError as e:
            flash(f"Login failed: {e.message}", "danger")
            
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        if not email or not password:
            flash("Email and password required", "danger")
            return render_template("login.html")
# ... [TRUNCATED -- preserve when editing] ...