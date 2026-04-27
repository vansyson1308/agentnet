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

@app.route("/agents/new", methods=["GET", "POST"])
def new_agent_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        endpoint = request.form.get("endpoint", "").strip()
        public_key = request.form.get("public_key", "").strip()
        capabilities_raw = request.form.get("capabilities", "[]").strip()
        try:
            capabilities = json.loads(capabilities_raw)
        except json.JSONDecodeError:
            flash("Invalid JSON in capabilities field.", "danger")
            return render_template("new_agent.html",
                                   name=name, description=description,
                                   endpoint=endpoint, public_key=public_key,
                                   capabilities=capabilities_raw)
        data = {
            "name": name,
            "description": description,
            "endpoint": endpoint,
            "public_key": public_key,
            "capabilities": capabilities
        }
        try:
            agent = api_client.create_agent(data)
            flash("Agent created successfully!", "success")
            return redirect(url_for('agent_detail_page', agent_id=agent["id"]))
        except APIError as e:
            flash(f"Agent creation failed: {e.message}", "danger")
            return render_template("new_agent.html",
                                   name=name, description=description,
                                   endpoint=endpoint, public_key=public_key,
                                   capabilities=capabilities_raw)
    return render_template("new_agent.html",
                           name="", description="",
                           endpoint="", public_key="",
                           capabilities="[]")

@app.route("/agents/<agent_id>")
def agent_detail_page(agent_id):
    try:
        agent = api_client.get_agent(agent_id)
    except APIError as e:
        flash(f"Agent not found: {e.message}", "danger")
        return redirect(url_for('directory_page'))
    return render_template("agent_detail.html", agent=agent)

@app.route("/directory")
def directory_page():
    search = request.args.get("search")
    category = request.args.get("category")
    sort = request.args.get("sort")
    order = request.args.get("order")
    try:
        agents = api_client.fetch_agents(search=search, category=category,
                                        sort=sort, order=order)
    except APIError:
        agents = []
        flash("Could not load agent directory.", "warning")
    return render_template("directory.html", agents=agents,
                           search=search, category=category,
                           sort=sort, order=order)

# ---- Notifications routes ----
@app.route("/notifications")
def notifications_page():
    try:
        notifications = api_client.get_notifications()
    except APIError:
        notifications = []
        flash("Could not load notifications.", "warning")
    return render_template("notifications.html", notifications=notifications)

@app.route("/notifications/read/<id>", methods=["POST"])
def mark_read(id):
    try:
        api_client.mark_notification_read(id)
        flash("Marked as read.", "success")
    except APIError as e:
        flash(f"Failed to mark: {e.message}", "danger")
    return redirect(url_for('notifications_page'))

@app.route("/notifications/read-all", methods=["POST"])
def mark_all_read():
    try:
        api_client.mark_all_notifications_read()
        flash("All notifications marked as read.", "success")
    except APIError as e:
        flash(f"Failed to mark all: {e.message}", "danger")
    return redirect(url_for('notifications_page'))

# ---- Tasks, offers, etc. (assumed from truncated code, re‑adding placeholder) ----
@app.route("/tasks")
def tasks_page():
    status = request.args.get("status")
    try:
        tasks = api_client.get_tasks()
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
    except APIError:
        tasks = []
        flash("Could not load tasks.", "warning")
    return render_template("tasks.html", tasks=tasks, current_status=status)

@app.route("/offers")
def offers_page():
    # Placeholder – extend as needed
    return render_template("offers.html")

@app.route("/goals")
def goals_page():
    return render_template("goals.html")

@app.route("/improvements")
def improvements_page():
    return render_template("improvements.html")

@app.route("/memory")
def memory_page():
    return render_template("memory.html")

@app.route("/metaverse")
def metaverse_page():
    return render_template("metaverse.html")

@app.route("/collaboration")
def collaboration_page():
    try:
        threads = api_client.get_collaboration_threads()
    except APIError:
        threads = []
        flash("Could not load collaboration threads.", "warning")
    return render_template("collaboration.html", threads=threads)

@app.route("/collaboration/<thread_id>")
def collaboration_thread_page(thread_id):
    try:
        messages = api_client.get_collaboration_messages(thread_id)
    except APIError:
        messages = []
        flash("Could not load thread messages.", "warning")
    return render_template("collaboration_thread.html", messages=messages, thread_id=thread_id)