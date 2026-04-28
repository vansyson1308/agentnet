import os
import json
import uuid
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from .api_client import api_client, APIError, AuthRequiredError

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

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, APIError):
        flash(f"API Error: {e.message}", "danger")
        referer = request.headers.get("Referer")
        return redirect(referer or url_for('index'))
    app.logger.error(f"Unhandled Exception: {e}")
    flash("An unexpected backend error occurred.", "danger")
    return render_template("error.html", error=str(e)), 500

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
        flash("Could not load transaction history.", "warning")
        
    return render_template("wallet.html", wallets=wallets, transactions=transactions)

@app.route("/wallet/<wallet_id>/fund", methods=["POST"])
def fund_wallet(wallet_id):
    try:
        api_client.fund_wallet(wallet_id, 1000)
        flash("Successfully added 1,000 Dev Credits to wallet.", "success")
    except APIError as e:
        flash(f"Funding failed: {e.message}", "danger")
    return redirect(url_for('wallet_page'))

@app.route("/agents", methods=["GET"])
def my_agents_page():
    try:
        agents = api_client.get_my_agents()
    except APIError as e:
        flash(f"Could not load agents: {e.message}", "danger")
        agents = []
    return render_template("my_agents.html", agents=agents)

@app.route("/agent/<agent_id>", methods=["GET"])
def agent_detail(agent_id):
    try:
        agent = api_client.get_agent(agent_id)
    except APIError as e:
        flash(f"Could not load agent: {e.message}", "danger")
        return redirect(url_for('my_agents_page'))
    return render_template("agent_detail.html", agent=agent)

@app.route("/agent/create", methods=["GET", "POST"])
def create_agent_page():
    if request.method == "POST":
        # Gather form data
        data = {
            "name": request.form.get("name"),
            "description": request.form.get("description"),
            "endpoint": request.form.get("endpoint"),
            "capabilities": request.form.getlist("capabilities"),
            "pricing": request.form.get("pricing", type=float),
        }
        try:
            agent = api_client.create_agent(data)
            flash("Agent created successfully.", "success")
            return redirect(url_for('agent_detail', agent_id=agent.get("id")))
        except APIError as e:
            flash(f"Failed to create agent: {e.message}", "danger")
    return render_template("new_agent.html")

@app.route("/metaverse")
def metaverse_page():
    # Public agent listing
    try:
        agents = api_client.get_agents()
    except APIError:
        agents = []
        flash("Could not load agent listings.", "warning")
    return render_template("metaverse.html", agents=agents)

@app.route("/notifications")
def notifications_page():
    try:
        notifications = api_client.get_notifications()
    except APIError:
        notifications = []
    return render_template("notifications.html", notifications=notifications)

@app.route("/tasks")
def tasks_page():
    try:
        tasks = api_client.get_tasks()
    except APIError:
        tasks = []
    return render_template("tasks.html", tasks=tasks)

@app.route("/collaboration")
def collaboration_page():
    return render_template("collaboration.html")

@app.route("/offer/create", methods=["GET", "POST"])
def create_offer_page():
    if request.method == "POST":
        # Create offer logic
        flash("Offer created.", "success")
        return redirect(url_for('index'))
    return render_template("create_offer.html")

@app.route("/discover/<capability>")
def discover_agents(capability):
    try:
        result = api_client.discover_agents(capability)
        agents = result.get("recommendations", [])
    except APIError as e:
        flash(f"Discovery failed: {e.message}", "danger")
        agents = []
    return render_template("discover.html", capability=capability, agents=agents)