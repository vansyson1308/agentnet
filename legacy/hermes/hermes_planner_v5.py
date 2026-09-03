#!/usr/bin/env python3
"""
hermes_planner_v5.py — Planner agent for AgentNet pipeline.
Reads next open task from Paperclip API (instead of YAML file).
Runs every 30 seconds.
"""
import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error
import subprocess
import signal

# ── Config ──
PAPERCLIP_URL = os.environ.get("PAPERCLIP_URL", "http://localhost:3100")
AGENTNET_URL = os.environ.get("AGENTNET_URL", "http://localhost:8000")
COMPANY_ID = "bbb50bef-ce01-4cc8-aac9-e33dae6395c0"
BUILDER_SCRIPT = "/opt/agentnet/hermes_builder_v6.py"
QA_SCRIPT = "/opt/agentnet/hermes_qaagent_v6.py"
SHIP_LOG = "/opt/agentnet/SHIP_LOG.md"
BACKLOG_FILE = "/opt/agentnet/AGENT_BACKLOG.md"  # keep as secondary source

# AgentNet auth
AGENTNET_AGENT_ID = os.environ.get("HERMES_BRAIN_AGENT_ID", "e19ac0aa-ad8f-42c9-b234-9890bbec3f89")
BUILDER_AGENT_ID = "2224fb23-aa03-425e-b07f-7d3cf8fcfb60"  # Hermes_Builder
PLANNER_AGENT_ID = "2a6f9475-4457-4548-ae47-84efa5661c09"  # Hermes_Planner

# AgentNet credentials for chat API
AGENTNET_USER_EMAIL = os.environ.get("AGENTNET_USER_EMAIL", "annhien.dev@gmail.com")
AGENTNET_USER_PASSWORD = os.environ.get("AGENTNET_USER_PASSWORD", "")

# ── Logging ──
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [PlannerV5] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("planner-v5")

# ── State ──
_current_issue_id = None
_current_issue_title = ""
_retries = 0
_active_builder_pid = None
_active_qa_pid = None
_max_retries = 3


def _agentnet_post(path, data):
    """POST AgentNet API with auth token."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{AGENTNET_URL}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json"}
    )
    token = _get_agentnet_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:200]
        log.warning(f"AgentNet POST {path}: HTTP {e.code}: {err_body}")
        return None
    except Exception as e:
        log.warning(f"AgentNet POST {path}: {e}")
        return None

def _get_agentnet_token():
    """Get AgentNet JWT token."""
    data = f"username={AGENTNET_USER_EMAIL}&password={AGENTNET_USER_PASSWORD}"
    req = urllib.request.Request(
        f"{AGENTNET_URL}/v1/auth/user/login",
        data.encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode())
            return result.get("access_token", "")
    except Exception as e:
        log.warning(f"AgentNet login failed: {e}")
        return ""

def _paperclip_get(path):
    """GET Paperclip API."""
    try:
        req = urllib.request.Request(f"{PAPERCLIP_URL}/api{path}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"Paperclip GET {path}: {e}")
        return None


def _paperclip_patch(path, data):
    """PATCH Paperclip API. Path is relative to /api/."""
    body = json.dumps(data).encode()
    url = f"{PAPERCLIP_URL}/api{path}" if not path.startswith("/api") else f"{PAPERCLIP_URL}{path}"
    req = urllib.request.Request(
        url, data=body, method="PATCH",
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:200]
        log.warning(f"Paperclip PATCH {path}: HTTP {e.code}: {err_body}")
        return None
    except Exception as e:
        log.warning(f"Paperclip PATCH {path}: {e}")
        return None


def _read_yaml_backlog():
    """Read backlog from YAML file (fallback)."""
    try:
        with open(BACKLOG_FILE) as f:
            content = f.read()
        # Simple YAML parsing for backlog items
        import re
        items = re.findall(
            r'- id: (\S+)\s+.*?title: (.+?)\s+.*?status: (\S+)',
            content, re.DOTALL
        )
        return [{"id": m[0], "title": m[1].strip().strip('"').strip("'"), "status": m[2]} for m in items]
    except Exception as e:
        log.warning(f"YAML backlog read failed: {e}")
        return []


def get_next_task():
    """Get next open task from YAML backlog (primary). Paperclip is dead."""
    global _current_issue_id, _current_issue_title, _retries

    # Primary: YAML backlog (Paperclip API unreliable / wrong endpoint)
    yaml_items = _read_yaml_backlog()
    open_yaml = [i for i in yaml_items if i["status"] in ("open", "todo")]  # only truly open, not migrated-to-paperclip or dispatched
    if open_yaml:
        return {"source": "yaml", **open_yaml[0]}

    # Secondary: Paperclip API (try if available)
    issues = _paperclip_get(f"/companies/{COMPANY_ID}/issues")
    if issues and isinstance(issues, list):
        open_issues = [i for i in issues if i.get("status") in ("todo", "backlog")]
        if open_issues:
            issue = open_issues[0]
            return {
                "source": "paperclip",
                "id": issue["id"],
                "title": issue["title"],
                "description": issue.get("description", ""),
                "priority": issue.get("priority", "medium"),
                "issue_number": issue.get("issueNumber"),
            }

    return None


def dispatch_to_builder(task):
    """Dispatch task to Builder process."""
    global _active_builder_pid, _active_qa_pid, _retries, _current_issue_id, _current_issue_title

    task_id = task["id"]
    task_title = task["title"]
    task_desc = task.get("description", "")
    source = task.get("source", "paperclip")

    log.info(f"DISPATCH: [{task_id}] {task_title}")

    # Update Paperclip issue status -> in_progress
    if source == "paperclip":
        _paperclip_patch(f"/issues/{task_id}", {"status": "in_progress", "assigneeUserId": "admin-001"})

    _current_issue_id = task_id
    _current_issue_title = task_title
    _retries = 0

    # Send proposal to Builder via AgentNet chat
    # Format: content contains ```json block with spec (Builder's _parse_proposal expects this)
    spec_json = {
        "id": task_id,
        "title": task_title,
        "description": task_desc,
        "source": source,
        "paperclip_issue_id": task_id if source == "paperclip" else None,
        "paperclip_company_id": COMPANY_ID if source == "paperclip" else None,
    }
    proposal_content = f"Task from Paperclip: {task_title}\n\n```json\n{json.dumps(spec_json, indent=2)}\n```"
    proposal_data = {
        "to_agent_id": BUILDER_AGENT_ID,
        "from_agent_id": PLANNER_AGENT_ID,
        "message_type": "proposal",
        "title": task_title,
        "content": proposal_content,
        "from_agent_name": "Planner v5 (Paperclip)",
        "metadata": {
            "source": source,
            "task_id": task_id,
            "paperclip_issue_id": task_id if source == "paperclip" else None,
            "paperclip_company_id": COMPANY_ID if source == "paperclip" else None,
            "ts": time.time(),
        }
    }
    chat_result = _agentnet_post("/v1/chat/", proposal_data)
    if chat_result:
        log.info(f"Proposal sent via AgentNet chat: {chat_result.get('id', '?')[:12]}")
    else:
        log.warning("Failed to send proposal via AgentNet chat")

    # Mark in YAML backlog (if exists) as dispatched
    if source == "yaml":
        subprocess.run(
            ["sed", "-i", f's/status: open/status: dispatched/', BACKLOG_FILE],
            capture_output=True,
        )

    # Log to ship log
    with open(SHIP_LOG, "a") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] **{task_id}** DISPATCHED: {task_title} -- to builder\n")

    return {"status": "dispatched", "task_id": task_id}


def mark_blocked(task_id, reason, source="paperclip"):
    """Mark task as blocked after max retries."""
    log.warning(f"BLOCKED: [{task_id}] {reason}")
    if source == "paperclip":
        _paperclip_patch(f"/issues/{task_id}", {"status": "blocked"})

    with open(SHIP_LOG, "a") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] **{task_id}** BLOCKED: {_current_issue_title} -- {reason}\n")


def main_loop():
    """Main Planner loop."""
    global _retries, _current_issue_id, _current_issue_title

    log.info("Planner v5 started — reading tasks from Paperclip API")
    log.info(f"Paperclip URL: {PAPERCLIP_URL}")

    while True:
        try:
            task = get_next_task()
            if task:
                result = dispatch_to_builder(task)
                log.info(f"Dispatched: {result}")

                # Wait for builder to finish (poll every 5s, max 300s)
                timeout = 300
                waited = 0
                while waited < timeout:
                    # Check if current issue still in_progress in Paperclip
                    if task["source"] == "paperclip":
                        issue = _paperclip_get(f"/issues/{task['id']}")
                        if issue and issue.get("status") in ("done", "cancelled", "blocked"):
                            log.info(f"Issue {task['id']} resolved: {issue.get('status')}")
                            break

                    time.sleep(5)
                    waited += 5

                if waited >= timeout:
                    log.warning(f"Timeout waiting for {task['id']}")
            else:
                log.info("No pending tasks. Sleeping...")
                time.sleep(15)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
