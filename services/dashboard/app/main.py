import os
import uuid
from flask import Flask, render_template, request, redirect, url_for, flash, session
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
        flash(f"Could not load agents: {e.message}", "warning")
        agents = []
    return render_template("my_agents.html", agents=agents)

@app.route("/agents/new", methods=["GET", "POST"])
def new_agent_page():
    import json
    if request.method == "POST":
        name = request.form.get("name")
        description = request.form.get("description")
        endpoint = request.form.get("endpoint")
        public_key = request.form.get("public_key")
        capabilities_raw = request.form.get("capabilities", "[]")
        
        try:
            capabilities = json.loads(capabilities_raw)
            if not isinstance(capabilities, list):
                raise ValueError("Capabilities must be a JSON array.")
        except ValueError as e:
            flash(f"Invalid JSON in capabilities: {e}", "danger")
            return render_template("new_agent.html",
                                   name=name, description=description, 
                                   endpoint=endpoint, public_key=public_key, 
                                   capabilities=capabilities_raw)
        
        # Construct payload
        payload = {
            "name": name,
            "description": description,
            "endpoint": endpoint,
            "public_key": public_key,
            "capabilities": capabilities
        }
        
        try:
            api_client.create_agent(payload)
            flash("Agent created successfully!", "success")
            return redirect(url_for('my_agents_page'))
        except APIError as e:
            flash(f"Failed to create agent: {e.message}", "danger")
            return render_template("new_agent.html",
                                   name=name, description=description, 
                                   endpoint=endpoint, public_key=public_key, 
                                   capabilities=capabilities_raw)
            
    # Default minimum viable capabilities template
    default_caps = '''[
  {
    "name": "echo",
    "version": "1.0",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "price": 0
  }
]'''
    return render_template("new_agent.html", capabilities=default_caps)

@app.route("/directory", methods=["GET"])
def directory_page():
    capability = request.args.get("capability")
    try:
        if capability:
            # discover endpoint returns dict with 'recommendations'
            data = api_client.discover_agents(capability)
            agents = data.get("recommendations", [])
        else:
            agents = api_client.get_agents()
            # Sort locally loosely by verification / success if returned
            agents.sort(key=lambda a: a.get("verify_score", 0), reverse=True)
    except APIError as e:
        flash(f"Could not load directory: {e.message}", "warning")
        agents = []
        
    return render_template("directory.html", agents=agents, capability=capability)

@app.route("/agents/<agent_id>", methods=["GET"])
def agent_detail_page(agent_id):
    try:
        agent = api_client.get_agent(agent_id)
        my_agents = api_client.get_my_agents() # Needed to select caller_agent_id
    except APIError as e:
        flash(f"Could not load agent details: {e.message}", "danger")
        return redirect(url_for('directory_page'))
        
    return render_template("agent_detail.html", agent=agent, my_agents=my_agents)

@app.route("/agents/<agent_id>/simulate", methods=["POST"])
def simulate_task(agent_id):
    import json
    caller_agent_id = request.form.get("caller_agent_id")
    capability = request.form.get("capability")
    input_data_raw = request.form.get("input_data", "{}")
    max_budget = int(request.form.get("max_budget", 100))
    currency = request.form.get("currency", "credits")
    
    if not caller_agent_id or not capability:
        flash("Missing required fields.", "danger")
        return redirect(url_for('agent_detail_page', agent_id=agent_id))
        
    try:
        input_data = json.loads(input_data_raw)
        if not isinstance(input_data, dict):
            raise ValueError("Input data must be a JSON object.")
    except ValueError as e:
        flash(f"Invalid JSON input: {e}", "danger")
        return redirect(url_for('agent_detail_page', agent_id=agent_id))
        
    retry_of_id = request.form.get("retry_of_id")
    
    payload = {
        "caller_agent_id": caller_agent_id,
        "callee_agent_id": agent_id,
        "capability": capability,
        "input": input_data,
        "max_budget": max_budget,
        "currency": currency,
        "timeout_seconds": 300,
        "retry_of_id": retry_of_id if retry_of_id else None
    }
    
    try:
        resp = api_client.create_task(payload)
        task_id = resp.get("task_session_id")
        flash("Task execution initiated successfully.", "success")
        return redirect(url_for('task_status_page', task_id=task_id))
    except APIError as e:
        flash(f"Simulation failed: {e.message}", "danger")
        return redirect(url_for('agent_detail_page', agent_id=agent_id))

@app.route("/tasks/<task_id>", methods=["GET"])
def task_status_page(task_id):
    try:
        task = api_client.get_task(task_id)
    except APIError as e:
        flash(f"Could not load task: {e.message}", "danger")
        return redirect(url_for('tasks_page'))
        
    return render_template("task_status.html", task=task)

@app.route("/tasks/<task_id>/retry", methods=["GET"])
def task_retry_page(task_id):
    try:
        old_task = api_client.get_task(task_id)
        if old_task.get("status") not in ["failed", "timed_out"]:
            flash("Only failed or timed out tasks can be retried.", "warning")
            return redirect(url_for('task_status_page', task_id=task_id))
    except APIError as e:
        flash(f"Could not load task to retry: {e.message}", "danger")
        return redirect(url_for('tasks_page'))
        
    return render_template("task_retry.html", old_task=old_task)

@app.route("/notifications", methods=["GET"])
def notifications_page():
    try:
        notifications = api_client.get_notifications()
    except APIError as e:
        flash(f"Could not load notifications: {e.message}", "warning")
        notifications = []
        
    return render_template("notifications.html", notifications=notifications)

@app.route("/notifications/<id>/read", methods=["POST"])
def mark_read(id):
    try:
        api_client.mark_notification_read(id)
    except APIError:
        pass
    return redirect(url_for('notifications_page'))

@app.route("/notifications/read-all", methods=["POST"])
def mark_all_read():
    try:
        api_client.mark_all_notifications_read()
    except APIError:
        pass
    return redirect(url_for('notifications_page'))

@app.route("/api/events/current")
def api_events_current():
    if "access_token" not in session:
        return {"events": []}
        
    try:
        notifications = api_client.get_notifications()
        unread = [n for n in notifications if not n["is_read"]]
        events = []
        for n in unread:
            events.append({
                "id": str(n["id"]),
                "title": n["title"],
                "message": n["message"],
                "url": n.get("url")
            })
    except Exception:
        events = []
        
    return {"events": events[:10], "unread_count": len([n for n in notifications if not n["is_read"]]) if 'notifications' in locals() else 0}

@app.route("/offers", methods=["GET"])
def offers_page():
    page = int(request.args.get("page", 1))
    limit = 20
    skip = (page - 1) * limit
    
    try:
        offers = api_client.get_offers(skip=skip, limit=limit)
        my_agents = api_client.get_my_agents()
        my_agent_keys = {a["id"] for a in my_agents}
        
        # Categorize
        incoming = []
        outgoing = []
        for o in offers:
            if o.get("to_agent_id") in my_agent_keys:
                incoming.append(o)
            elif o.get("from_agent_id") in my_agent_keys:
                outgoing.append(o)
            else:
                outgoing.append(o) # Fallback
    except APIError as e:
        flash(f"Could not load offers: {e.message}", "warning")
        incoming = []
        outgoing = []
        
    has_next = len(offers) == limit
    return render_template("offers.html", incoming=incoming, outgoing=outgoing, page=page, has_next=has_next)

@app.route("/offers/<offer_id>", methods=["GET", "POST"])
def offer_detail_page(offer_id):
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "accept":
                api_client.accept_offer(offer_id)
                flash("Offer accepted! Escrow will lock upon task creation.", "success")
            elif action == "reject":
                api_client.reject_offer(offer_id)
                flash("Offer rejected.", "info")
            elif action == "counter":
                price = int(request.form.get("proposed_price"))
                terms = request.form.get("proposed_terms", "")
                payload = {"proposed_price": price, "proposed_terms": terms}
                api_client.counter_offer(offer_id, payload)
                flash("Counter-offer submitted.", "success")
        except APIError as e:
            flash(f"Action failed: {e.message}", "danger")
        return redirect(url_for('offer_detail_page', offer_id=offer_id))
        
    try:
        offer = api_client.get_offer(offer_id)
        my_agents = [a["id"] for a in api_client.get_my_agents()]
        is_recipient = offer.get("to_agent_id") in my_agents
    except APIError as e:
        flash(f"Could not load offer: {e.message}", "danger")
        return redirect(url_for('offers_page'))
        
    return render_template("offer_detail.html", offer=offer, is_recipient=is_recipient, my_agents=my_agents)

@app.route("/agents/<agent_id>/offer", methods=["GET", "POST"])
def create_offer_page(agent_id):
    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description", "")
        price = int(request.form.get("price", 0))
        
        # Calculate expiration (e.g. +24 hours)
        from datetime import datetime, timedelta, timezone
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        
        caller_agent_id = request.form.get("caller_agent_id")
        
        payload = {
            "to_agent_id": agent_id,
            "caller_agent_id": caller_agent_id,
            "title": title,
            "description": description,
            "price": price,
            "currency": "credits",
            "expires_at": expires
        }
        try:
            offer = api_client.create_offer(payload)
            flash("Offer submitted successfully.", "success")
            return redirect(url_for('offer_detail_page', offer_id=offer.get('id')))
        except APIError as e:
            flash(f"Failed to submit offer: {e.message}", "danger")
            
    try:
        callee = api_client.get_agent(agent_id)
        my_agents = api_client.get_my_agents()
    except APIError as e:
        flash(f"Could not load agent details: {e.message}", "warning")
        return redirect(url_for('directory_page'))
        
    return render_template("create_offer.html", callee=callee, my_agents=my_agents)

@app.route("/tasks", methods=["GET"])
def tasks_page():
    status_filter = request.args.get("status")
    page = int(request.args.get("page", 1))
    limit = 20
    skip = (page - 1) * limit
    
    try:
        tasks = api_client.get_tasks(status=status_filter, skip=skip, limit=limit)
    except APIError as e:
        # Gracefully handle "No agent found" if user hasn't created an agent yet.
        if e.status_code == 403 and "No agent found" in str(e.message):
            tasks = []
        else:
            flash(f"Could not load tasks: {e.message}", "danger")
            tasks = []
            
    has_next = len(tasks) == limit
    return render_template("tasks.html", tasks=tasks, current_status=status_filter, page=page, has_next=has_next)

@app.route("/tasks/<task_id>/trace", methods=["GET"])
def task_trace_page(task_id):
    try:
        task = api_client.get_task(task_id)
        trace_id = task.get("trace_id")
        if not trace_id:
            flash("Trace ID not found for this task.", "danger")
            return redirect(url_for('task_status_page', task_id=task_id))
            
        trace_data = api_client.get_trace(trace_id)
        spans = trace_data.get("spans", [])
        
        # Build span tree
        span_map = { s["span_id"]: dict(s, children=[]) for s in spans }
        root_spans = []
        for s in spans:
            node = span_map[s["span_id"]]
            parent_id = s.get("parent_span_id")
            if parent_id and parent_id in span_map:
                span_map[parent_id]["children"].append(node)
            else:
                root_spans.append(node)
                
    except APIError as e:
        flash(f"Could not load trace: {e.message}", "danger")
        return redirect(url_for('task_status_page', task_id=task_id))
        
    return render_template("task_trace.html", task=task, root_spans=root_spans, trace_id=trace_id)

if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
