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
    except APIError:
        agents = []
        flash("Could not load agents.", "warning")
    return render_template("my_agents.html", agents=agents)

@app.route("/metaverse")
def metaverse_page():
    """Display the Metaverse overview page."""
    return render_template("metaverse.html")

# ---- The remaining routes (truncated in the original) are assumed to exist -----
# For completeness, we keep the placeholder to ensure the file is valid.
# The following routes are expected to be present in the original code:
@app.route("/agents/<agent_id>")
def agent_detail_page(agent_id):
    try:
        agent = api_client.get_agent(agent_id)
        return render_template("agent_detail.html", agent=agent)
    except APIError:
        flash("Agent not found", "danger")
        return redirect(url_for('directory_page'))

@app.route("/directory")
def directory_page():
    try:
        agents = api_client.get_agents()
    except APIError:
        agents = []
        flash("Could not load agent directory", "warning")
    return render_template("directory.html", agents=agents)

@app.route("/offers")
def offers_page():
    return render_template("offers.html")

@app.route("/goals")
def goals_page():
    return render_template("goals.html")

@app.route("/goals/<goal_id>")
def goal_detail_page(goal_id):
    return render_template("goal_detail.html", goal_id=goal_id)

@app.route("/improvements")
def improvements_page():
    return render_template("improvements.html")

@app.route("/improvements/<improvement_id>")
def improvement_detail_page(improvement_id):
    return render_template("improvement_detail.html", improvement_id=improvement_id)

@app.route("/memory")
def memory_page():
    return render_template("memory.html")

@app.route("/tasks")
def tasks_page():
    try:
        tasks = api_client.get_tasks()
    except APIError:
        tasks = []
        flash("Could not load tasks", "warning")
    return render_template("tasks.html", tasks=tasks)

@app.route("/collaboration")
def collaboration_page():
    return render_template("collaboration.html")

@app.route("/collaboration/<thread_id>")
def collaboration_thread_page(thread_id):
    return render_template("collaboration_thread.html", thread_id=thread_id)

@app.route("/agents/new", methods=["GET", "POST"])
def new_agent_page():
    if request.method == "POST":
        data = {
            "name": request.form.get("name"),
            "description": request.form.get("description"),
            "endpoint": request.form.get("endpoint"),
            "public_key": request.form.get("public_key"),
            "capabilities": json.loads(request.form.get("capabilities", "[]"))
        }
        try:
            api_client.create_agent(data)
            flash("Agent registered successfully.", "success")
            return redirect(url_for('my_agents_page'))
        except APIError as e:
            flash(f"Failed to register agent: {e.message}", "danger")
            return render_template("new_agent.html", **request.form)
    return render_template("new_agent.html")

@app.route("/offer/<callee_id>", methods=["GET", "POST"])
def create_offer_page(callee_id):
    try:
        callee = api_client.get_agent(callee_id)
        my_agents = api_client.get_my_agents()
    except APIError:
        flash("Agent not found", "danger")
        return redirect(url_for('directory_page'))
    if request.method == "POST":
        # handle offer creation
        pass
    return render_template("create_offer.html", callee=callee, my_agents=my_agents)