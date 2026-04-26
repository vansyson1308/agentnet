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
        flash(f"Could not load agents: {e.message}", "warning")
        return redirect(url_for('index'))
    return render_template("my_agents.html", agents=agents)

# New route: public marketplace landing page
@app.route("/marketplace")
def marketplace_page():
    return render_template("marketplace.html")


# ─────────────────────────────────────────────────────
# Goals + Improvements + Memory + Mission
# (Phase: agent-goals-and-self-improvement)
# ─────────────────────────────────────────────────────


@app.route("/goals", methods=["GET"])
def goals_page():
    """Society-wide goal map. Public read; create gated by login."""
    status = request.args.get("status") or None
    owner_type = request.args.get("owner_type") or None
    try:
        goals = api_client.get_goals(status=status, owner_type=owner_type, limit=200)
    except APIError as e:
        flash(f"Could not load goals: {e.message}", "warning")
        goals = []

    my_agents = []
    if "access_token" in session:
        try:
            my_agents = api_client.get_my_agents()
        except APIError:
            my_agents = []
    return render_template(
        "goals.html",
        goals=goals,
        my_agents=my_agents,
        filter_status=status,
        filter_owner_type=owner_type,
    )


@app.route("/goals/new", methods=["POST"])
def create_goal_route():
    if "access_token" not in session:
        flash("Please log in to create a goal.", "warning")
        return redirect(url_for("login_page"))

    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Goal title cannot be empty.", "danger")
        return redirect(url_for("goals_page"))

    owner_type = request.form.get("owner_type") or "AGENT"
    owner_id = request.form.get("owner_id") or None
    if not owner_id:
        flash("Owner is required.", "danger")
        return redirect(url_for("goals_page"))

    success_criteria = [
        line.strip()
        for line in (request.form.get("success_criteria") or "").splitlines()
        if line.strip()
    ]

    payload = {
        "title": title,
        "description": (request.form.get("description") or "").strip() or None,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "priority": request.form.get("priority") or "medium",
        "success_criteria": success_criteria,
    }
    try:
        api_client.create_goal(payload)
        flash("Goal created.", "success")
    except APIError as e:
        flash(f"Could not create goal: {e.message}", "danger")
    return redirect(url_for("goals_page"))


@app.route("/goals/<goal_id>", methods=["GET"])
def goal_detail_page(goal_id):
    try:
        detail = api_client.get_goal(goal_id)
    except APIError as e:
        flash(f"Could not load goal: {e.message}", "warning")
        return redirect(url_for("goals_page"))
    return render_template("goal_detail.html", detail=detail)


@app.route("/goals/<goal_id>/<action>", methods=["POST"])
def goal_action(goal_id, action):
    if "access_token" not in session:
        return redirect(url_for("login_page"))
    try:
        if action == "complete":
            api_client.complete_goal(goal_id)
            flash("Goal marked completed.", "success")
        elif action == "fail":
            api_client.fail_goal(goal_id)
            flash("Goal marked failed.", "info")
        elif action == "cancel":
            api_client.cancel_goal(goal_id)
            flash("Goal cancelled.", "info")
        else:
            flash(f"Unknown action: {action}", "danger")
    except APIError as e:
        flash(f"Action failed: {e.message}", "danger")
    return redirect(url_for("goal_detail_page", goal_id=goal_id))


@app.route("/improvements", methods=["GET"])
def improvements_page():
    """Improvement Lab — auto-generated proposals + lifecycle controls."""
    status_filter = request.args.get("status") or None
    source = request.args.get("source") or None
    try:
        proposals = api_client.get_improvements(status=status_filter, source=source, limit=200)
    except APIError as e:
        flash(f"Could not load proposals: {e.message}", "warning")
        proposals = []
    return render_template(
        "improvements.html",
        proposals=proposals,
        filter_status=status_filter,
        filter_source=source,
    )


@app.route("/improvements/<proposal_id>", methods=["GET"])
def improvement_detail_page(proposal_id):
    try:
        proposal = api_client.get_improvement(proposal_id)
    except APIError as e:
        flash(f"Could not load proposal: {e.message}", "warning")
        return redirect(url_for("improvements_page"))

    my_agents = []
    if "access_token" in session:
        try:
            my_agents = api_client.get_my_agents()
        except APIError:
            my_agents = []
    return render_template(
        "improvement_detail.html",
        proposal=proposal,
        my_agents=my_agents,
    )


@app.route("/improvements/<proposal_id>/<action>", methods=["POST"])
def improvement_action(proposal_id, action):
    if "access_token" not in session:
        return redirect(url_for("login_page"))
    try:
        if action == "approve":
            api_client.approve_improvement(proposal_id)
            flash("Proposal approved.", "success")
        elif action == "reject":
            reason = (request.form.get("reason") or "").strip() or None
            api_client.reject_improvement(proposal_id, reason=reason)
            flash("Proposal rejected.", "info")
        elif action == "convert":
            payload = {
                "callee_agent_id": (request.form.get("callee_agent_id") or "").strip(),
                "capability": (request.form.get("capability") or "").strip(),
                "escrow_amount": int(request.form.get("escrow_amount") or 0),
                "timeout_minutes": int(request.form.get("timeout_minutes") or 60),
                "input": {},
            }
            if not payload["callee_agent_id"]:
                flash("Callee agent is required.", "danger")
                return redirect(url_for("improvement_detail_page", proposal_id=proposal_id))
            if not payload["capability"]:
                flash("Capability is required.", "danger")
                return redirect(url_for("improvement_detail_page", proposal_id=proposal_id))
            api_client.convert_improvement(proposal_id, payload)
            flash("Proposal converted to a task.", "success")
        elif action == "mark-implemented":
            api_client.mark_improvement_implemented(proposal_id)
            flash("Proposal marked as IMPLEMENTED.", "success")
        else:
            flash(f"Unknown action: {action}", "danger")
    except (APIError, ValueError) as e:
        flash(f"Action failed: {e}", "danger")
    return redirect(url_for("improvement_detail_page", proposal_id=proposal_id))


@app.route("/memory", methods=["GET"])
def memory_page():
    """Society and agent-scope lessons."""
    scope = request.args.get("scope") or None
    tag = request.args.get("tag") or None
    try:
        items = api_client.get_memory(scope=scope, tag=tag, limit=200)
    except APIError as e:
        flash(f"Could not load memory: {e.message}", "warning")
        items = []
    my_agents = []
    if "access_token" in session:
        try:
            my_agents = api_client.get_my_agents()
        except APIError:
            my_agents = []
    return render_template(
        "memory.html",
        items=items,
        my_agents=my_agents,
        filter_scope=scope,
        filter_tag=tag,
    )


@app.route("/memory/new", methods=["POST"])
def create_memory_route():
    if "access_token" not in session:
        return redirect(url_for("login_page"))

    scope = request.form.get("scope") or "SOCIETY"
    try:
        importance = int(request.form.get("importance") or 50)
    except ValueError:
        importance = 50
    payload = {
        "title": (request.form.get("title") or "").strip(),
        "content": (request.form.get("content") or "").strip(),
        "scope": scope,
        "tags": [
            t.strip().lower()
            for t in (request.form.get("tags") or "").split(",")
            if t.strip()
        ],
        "importance": max(0, min(100, importance)),
    }
    if scope == "AGENT":
        agent_id = (request.form.get("agent_id") or "").strip() or None
        if not agent_id:
            flash("Agent-scope memory requires selecting an agent.", "danger")
            return redirect(url_for("memory_page"))
        payload["agent_id"] = agent_id
    if not payload["title"] or not payload["content"]:
        flash("Title and content are required.", "danger")
        return redirect(url_for("memory_page"))
    try:
        api_client.create_memory(payload)
        flash("Memory item written.", "success")
    except APIError as e:
        flash(f"Could not write memory: {e.message}", "danger")
    return redirect(url_for("memory_page"))


@app.route("/memory/<memory_id>/delete", methods=["POST"])
def delete_memory_route(memory_id):
    if "access_token" not in session:
        return redirect(url_for("login_page"))
    try:
        api_client.delete_memory(memory_id)
        flash("Memory item deleted.", "info")
    except APIError as e:
        flash(f"Could not delete: {e.message}", "danger")
    return redirect(url_for("memory_page"))


@app.route("/agents/<agent_id>/mission", methods=["GET", "POST"])
def agent_mission_page(agent_id):
    """View / edit one agent's mission and active goal."""
    if request.method == "POST":
        if "access_token" not in session:
            return redirect(url_for("login_page"))
        mission = request.form.get("mission")
        clear_goal = request.form.get("clear_goal") == "1"
        current_goal_id = request.form.get("current_goal_id") or None
        try:
            api_client.update_agent_mission(
                agent_id,
                mission=mission,
                current_goal_id=current_goal_id,
                clear_goal=clear_goal,
            )
            flash("Mission updated.", "success")
        except APIError as e:
            flash(f"Update failed: {e.message}", "danger")
        return redirect(url_for("agent_mission_page", agent_id=agent_id))

    try:
        mission = api_client.get_agent_mission(agent_id)
    except APIError as e:
        flash(f"Could not load mission: {e.message}", "warning")
        return redirect(url_for("my_agents_page"))
    try:
        agent_goals = api_client.get_agent_goals(agent_id)
    except APIError:
        agent_goals = []
    try:
        lessons = api_client.get_agent_lessons(agent_id, limit=20)
    except APIError:
        lessons = []
    return render_template(
        "agent_mission.html",
        mission=mission,
        agent_id=agent_id,
        agent_goals=agent_goals,
        lessons=lessons,
    )


# ── Werewolf Arena Routes ──
WEREWOLF_STATE_FILE = os.environ.get("WEREWOLF_STATE_FILE", "/opt/agentnet/werewolf_state.json")

@app.route("/werewolf")
def werewolf_arena():
    """Werewolf Arena spectator page — public, no login needed."""
    return render_template("werewolf_arena.html")

@app.route("/werewolf/data")
def werewolf_data():
    """JSON endpoint for live game state — public, no login needed."""
    state = {}
    try:
        if os.path.exists(WEREWOLF_STATE_FILE):
            with open(WEREWOLF_STATE_FILE) as f:
                state = json.load(f)
    except Exception:
        state = {"error": "Could not load game state"}
    return jsonify(state)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
