import os
import json
import uuid
import time
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, stream_with_context
from .api_client import api_client, APIError, AuthRequiredError
import pathlib
import typing as _typing

app = Flask(__name__)
# In production, this should be a secure random string stored in env vars
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
    elif timeouts > (total * 0.1): # more than 10% timeouts
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
    """Return JSON for 404 instead of HTML error page."""
    app.logger.warning(f"404: {request.path}")
    if request.path.startswith("/werewolf") or request.path.startswith("/api"):
        return jsonify({"error": "not_found", "path": request.path}), 404
    flash("Page not found.", "warning")
    return redirect(url_for('metaverse_page'))

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, APIError):
        flash(f"API Error: {e.message}", "danger")
        referer = request.headers.get("Referer")
        return redirect(referer or url_for('index'))
    if isinstance(e, (404,)):
        return handle_not_found(e)
    app.logger.error(f"Unhandled Exception: {e}")
    # Favicon, robots → always 204 no content
    if request.path in ("/favicon.ico", "/robots.txt"):
        return "", 204
    if request.path.startswith("/api"):
        return jsonify({"error": "internal_error"}), 500
    flash("An unexpected backend error occurred.", "danger")
    return render_template("error.html", error=str(e) if app.debug else "Internal Server Error"), 500

@app.context_processor
def inject_user():
    return dict(is_logged_in="access_token" in session)

# ---- Public landing page ----
@app.route("/landing")
def landing_page():
    return render_template("landing.html")

@app.route("/")
def index():
    if "access_token" not in session:
        return redirect(url_for('metaverse_page'))
    
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
            return render_template("register.html")
            
        try:
            api_client.register(email, password)
            flash("Account created! Please log in.", "success")
            return redirect(url_for('login_page'))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
            
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('login_page'))

@app.route("/wallet", methods=["GET"])
def wallet_page():
    wallets = api_client.get_wallets()
    transactions = []
    try:
        transactions = api_client.get_transactions()
    except APIError:
        flash()
# ... [TRUNCATED -- preserve when editing] ...

# ---- Dashboard Stats API (real-time) ----
@app.route("/api/dashboard/stats")
def dashboard_stats_api():
    """Return JSON with real-time dashboard metrics."""
    if "access_token" not in session:
        return jsonify({"error": "unauthorized"}), 401
    try:
        stats = api_client.get_dashboard_stats()
        return jsonify(stats)
    except APIError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        app.logger.error(f"dashboard_stats error: {e}")
        return jsonify({"error": "internal_error"}), 500

@app.route("/api/dashboard/stats/stream")
def dashboard_stats_stream():
    """SSE endpoint that pushes dashboard stats every 10 seconds."""
    if "access_token" not in session:
        return jsonify({"error": "unauthorized"}), 401
    def generate():
        while True:
            try:
                stats = api_client.get_dashboard_stats()
                yield f"data: {json.dumps(stats)}\n\n"
            except Exception as e:
                app.logger.error(f"SSE error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(10)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')