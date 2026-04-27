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
        # Go back to referring page or index
        referer = request.headers.get("Referer")
        return redirect(referer or url_for('index'))
    # General fallback
    app.logger.error(f"Unhandled Exception: {e}")
    flash("An unexpected backend error occurred.", "danger")
    return render_template("error.html", error=str(e)), 500

@app.context_processor
def inject_user():
    return dict(is_logged_in="access_token" in session)

@app.route("/")
def index():
    if "access_token" not in session:
        return redirect(url_for('login_page'))
    
    # Get basic overview from wallets (used as proxy for account activity)
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
    # Show wallets and history
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
        # Hardcoding dev top-up to 1000 credits
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

@app.route("/agents/new", methods=["GET", "POST"])
def new_agent_page():
    if request.method == "POST":
        name = request.form["name"]
        endpoint = request.form["endpoint"]
        public_key = request.form["public_key"]
        capabilities_str = request.form["capabilities"]
        try:
            capabilities = json.loads(capabilities_str)
        except json.JSONDecodeError:
            flash("Invalid JSON for capabilities.", "danger")
            return render_template("new_agent.html", name=name, endpoint=endpoint, public_key=public_key, capabilities=capabilities_str)
        data = {
            "name": name,
            "endpoint": endpoint,
            "public_key": public_key,
            "capabilities": capabilities
        }
        try:
            api_client.create_agent(data)
            flash("Agent registered successfully.", "success")
            return redirect(url_for('my_agents_page'))
        except APIError as e:
            flash(f"Registration failed: {e.message}", "danger")
            return render_template("new_agent.html", name=name, endpoint=endpoint, public_key=public_key, capabilities=capabilities_str)
    return render_template("new_agent.html", capabilities='[{"name": "example", "price": 10}]')

@app.route("/directory")
def directory_page():
    search = request.args.get("search")
    category = request.args.get("category")
    sort = request.args.get("sort")
    order = request.args.get("order")
    agents = api_client.fetch_agents(search=search, category=category, sort=sort, order=order)
    return render_template("directory.html", agents=agents, search=search, category=category, sort=sort, order=order)

@app.route("/agents/<agent_id>")
def agent_detail_page(agent_id):
    try:
        agent = api_client.get_agent(agent_id)
    except APIError as e:
        flash(f"Could not load agent: {e.message}", "danger")
        return redirect(url_for('directory_page'))
    # Get offers related to this agent
    offers = []
    try:
        offers = api_client.get_offers_for_agent(agent_id)
    except APIError:
        pass
    return render_template("agent_detail.html", agent=agent, offers=offers)

@app.route("/offers")
def offers_page():
    try:
        offers = api_client.get_offers()
    except APIError as e:
        flash(f"Could not load offers: {e.message}", "danger")
        offers = []
    return render_template("offers.html", offers=offers)

@app.route("/offers/create/<callee_id>", methods=["GET", "POST"])
def create_offer_page(callee_id):
    try:
        callee = api_client.get_agent(callee_id)
    except APIError:
        flash("Target agent not found.", "danger")
        return redirect(url_for('directory_page'))
    my_agents = []
    try:
        my_agents = api_client.get_my_agents()
    except APIError:
        flash("Could not load your agents.", "warning")
    
    if request.method == "POST":
        caller_agent_id = request.form["caller_agent_id"]
        title = request.form["title"]
        description = request.form.get("description", "")
        price = int(request.form["price"])
        try:
            api_client.create_offer(caller_agent_id, callee_id, title, description, price)
            flash("Offer proposed successfully.", "success")
            return redirect(url_for('offers_page'))
        except APIError as e:
            flash(f"Offer creation failed: {e.message}", "danger")
    
    return render_template("create_offer.html", callee=callee, my_agents=my_agents)

@app.route("/goals")
def goals_page():
    try:
        goals = api_client.get_goals()
    except APIError as e:
        flash(f"Could not load goals: {e.message}", "danger")
        goals = []
    return render_template("goals.html", goals=goals)

@app.route("/goals/<goal_id>")
def goal_detail_page(goal_id):
    try:
        goal = api_client.get_goal(goal_id)
        agents = api_client.get_my_agents()
    except APIError as e:
        flash(f"Could not load goal: {e.message}", "danger")
        return redirect(url_for('goals_page'))
    return render_template("goal_detail.html", goal=goal, agents=agents)

@app.route("/improvements")
def improvements_page():
    try:
        improvements = api_client.get_improvements()
    except APIError as e:
        flash(f"Could not load improvements: {e.message}", "danger")
        improvements = []
    return render_template("improvements.html", improvements=improvements)

@app.route("/improvements/<improvement_id>")
def improvement_detail_page(improvement_id):
    try:
        improvement = api_client.get_improvement(improvement_id)
    except APIError as e:
        flash(f"Could not load improvement: {e.message}", "danger")
        return redirect(url_for('improvements_page'))
    return render_template("improvement_detail.html", improvement=improvement)

@app.route("/memory")
def memory_page():
    try:
        memories = api_client.get_memories()
    except APIError as e:
        flash(f"Could not load memories: {e.message}", "danger")
        memories = []
    return render_template("memory.html", memories=memories)

@app.route("/metaverse")
def metaverse_page():
    return render_template("metaverse.html")

# --- Notifications routes ---

@app.route("/notifications", methods=["GET"])
def notifications_page():
    try:
        notifications = api_client.get_notifications()
    except APIError as e:
        flash(f"Could not load notifications: {e.message}", "danger")
        notifications = []
    return render_template("notifications.html", notifications=notifications)

@app.route("/notifications/<id>/read", methods=["POST"])
def mark_read(id):
    try:
        api_client.mark_notification_read(id)
    except APIError as e:
        flash(f"Could not mark notification: {e.message}", "danger")
    return redirect(url_for('notifications_page'))

@app.route("/notifications/mark_all_read", methods=["POST"])
def mark_all_read():
    try:
        api_client.mark_all_notifications_read()
    except APIError as e:
        flash(f"Could not mark notifications: {e.message}", "danger")
    return redirect(url_for('notifications_page'))

# --- Task sessions ---

@app.route("/tasks", methods=["GET"])
def tasks_page():
    status_filter = request.args.get("status")
    try:
        tasks = api_client.get_tasks()
    except APIError as e:
        flash(f"Could not load tasks: {e.message}", "danger")
        tasks = []
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    return render_template("tasks.html", tasks=tasks, current_status=status_filter)

# --- Collaboration ---

@app.route("/collaboration")
def collaboration_page():
    try:
        threads = api_client.get_collaboration_threads()
    except APIError as e:
        flash(f"Could not load collaboration threads: {e.message}", "danger")
        threads = []
    return render_template("collaboration.html", threads=threads)

@app.route("/collaboration/<thread_id>")
def collaboration_thread_page(thread_id):
    try:
        thread = api_client.get_collaboration_thread(thread_id)
    except APIError as e:
        flash(f"Could not load thread: {e.message}", "danger")
        return redirect(url_for('collaboration_page'))
    return render_template("collaboration_thread.html", thread=thread)