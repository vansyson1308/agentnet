#!/usr/bin/env python3
"""
Hermes_QAAgent v6 -- Real Acceptance Test Runner

Diff vs v5:
- v5 ran 5 generic endpoint pings for every proposal -> always "4/5 PASS".
- v6 reads the backlog item's acceptance criteria (each is a shell-runnable
  assertion or a curl + JSON pattern), executes them one by one, returns
  concrete PASS/FAIL with the failing assertion.

Acceptance format examples (just shell commands or curl + grep):
  - "curl -s http://127.0.0.1:8000/v1/health/deep | grep -q '\"ok\"'"
  - "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/agents/.../capabilities | grep -q '^200$'"
  - "test -f services/dashboard/app/static/css/dark.css"
  - "grep -q 'task-chart-7d' services/dashboard/app/templates/home.html"

Each acceptance string is run as: bash -c '<acceptance>'.
Exit 0 = pass, non-zero = fail.

If services need restart for code change to take effect, QA does
`docker compose -f /opt/agentnet/docker-compose.prod.yml restart registry dashboard`
before running HTTP-based criteria.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time as time_module
import urllib.request
import yaml
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS  # noqa: E402

REPO_ROOT = pathlib.Path("/opt/agentnet")
BACKLOG_PATH = REPO_ROOT / "AGENT_BACKLOG.md"
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"


def _load_backlog_item(item_id: str) -> Optional[dict]:
    if not BACKLOG_PATH.exists():
        return None
    text = BACKLOG_PATH.read_text(encoding="utf-8")
    m = re.search(r"```yaml\n(.+?)\n```", text, re.DOTALL)
    if not m:
        return None
    data = yaml.safe_load(m.group(1)) or {}
    for it in data.get("backlog", []):
        if it.get("id") == item_id:
            return it
    return None


def _get_token() -> str:
    """Auth token for substituting $TOKEN in acceptance commands."""
    data = b"username=CHANGE_ME@example.com&password=CHANGE_ME"
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/auth/user/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("access_token", "")
    except Exception:
        return ""


def _list_agent_ids() -> dict[str, str]:
    """Return {agent_name: agent_id} for substitution."""
    token = _get_token()
    if not token:
        return {}
    req = urllib.request.Request(
        "http://127.0.0.1:8000/v1/agents/?limit=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            agents = json.loads(r.read().decode())
            return {a.get("name", ""): a.get("id", "") for a in agents}
    except Exception:
        return {}


class QAAgentV6(HermesAgent):
    def __init__(self):
        super().__init__("Hermes_QAAgent_v6", "qa", sleep_seconds=20)
        self._restart_attempted_for = set()  # backlog ids we've already restarted services for

    def on_start(self):
        self.load_processed()
        self.log.info("QAAgent_v6 starting -- backlog at %s", BACKLOG_PATH)

    def _maybe_restart_services(self, item_id: str, files_modified: list[str]) -> None:
        """If modified files are in registry/dashboard, restart those containers once."""
        if item_id in self._restart_attempted_for:
            return
        self._restart_attempted_for.add(item_id)
        services_to_restart = set()
        for f in files_modified:
            if "services/registry" in f:
                services_to_restart.add("registry")
            elif "services/dashboard" in f:
                services_to_restart.add("dashboard")
            elif "services/payment" in f:
                services_to_restart.add("payment")
            elif "services/worker" in f:
                services_to_restart.add("worker")
        if not services_to_restart or not COMPOSE_FILE.exists():
            return
        cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "restart"] + sorted(services_to_restart)
        self.log.info("restarting docker services: %s", services_to_restart)
        try:
            subprocess.run(cmd, timeout=60, capture_output=True)
            time_module.sleep(8)  # let containers come back
        except Exception as e:
            self.log.warning("restart failed: %s", e)

    def _run_acceptance(self, criterion: str, env: dict[str, str]) -> tuple[bool, str]:
        """Run criterion as shell command. Return (passed, output)."""
        try:
            r = subprocess.run(
                ["bash", "-c", criterion],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, **env, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                cwd=str(REPO_ROOT),
            )
            output = (r.stdout + "\n" + r.stderr).strip()[:500]
            return r.returncode == 0, output
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT (>30s)"
        except Exception as e:
            return False, f"exec error: {e}"

    def _process_review_request(self, msg: dict) -> None:
        title = msg.get("title", "") or ""
        content = msg.get("content", "") or ""
        # Extract backlog id
        m = re.search(r"\b(AB-\d{3,})\b", title + " " + content)
        if not m:
            self.log.warning("review_request without backlog id: %s", title[:60])
            return
        backlog_id = m.group(1)
        item = _load_backlog_item(backlog_id)
        if not item:
            self.log.warning("backlog item %s not found", backlog_id)
            self._send_review_result(
                backlog_id,
                "Backlog item not found in AGENT_BACKLOG.md",
                False,
                msg.get("thread_id") or msg.get("id"),
            )
            return

        criteria = item.get("acceptance", [])
        files_modified = item.get("files_to_modify", [])

        # Restart services if needed (HTTP-touching changes)
        if any("curl" in c for c in criteria):
            self._maybe_restart_services(backlog_id, files_modified)

        # Build env for substitution
        token = _get_token()
        agents_map = _list_agent_ids()
        env = {"TOKEN": token}
        # Common agent id shortcuts
        if "hermes-brain" in agents_map:
            env["AGENT_ID"] = agents_map["hermes-brain"]
        if "openclaw-workhorse" in agents_map:
            env["OPENCLAW_ID"] = agents_map["openclaw-workhorse"]

        results: list[tuple[str, bool, str]] = []
        for c in criteria:
            ok, out = self._run_acceptance(c, env)
            results.append((c, ok, out))
            self.log.info("  [%s] %s", "PASS" if ok else "FAIL", c[:80])

        passed = all(r[1] for r in results) and len(results) > 0

        # Format report
        report_lines = [
            f"QA verdict for {backlog_id}: {'PASSED' if passed else 'FAILED'}",
            f"Tested {len(results)} acceptance criteria.",
            "",
        ]
        for c, ok, out in results:
            mark = "PASS" if ok else "FAIL"
            report_lines.append(f"[{mark}] {c}")
            if not ok and out:
                report_lines.append(f"   output: {out[:200]}")

        self._send_review_result(
            backlog_id,
            "\n".join(report_lines),
            passed,
            msg.get("thread_id") or msg.get("id"),
        )

    def _send_review_result(self, backlog_id: str, body: str, passed: bool, thread_id: str) -> None:
        title = f"REVIEW {backlog_id} -- {'PASSED' if passed else 'FAILED'}"
        self.send_msg("planner", "review_result", title, body, thread_id=thread_id)
        self.log.info("sent review_result %s -> planner", title)

    def on_tick(self):
        msgs = self.api_get(
            f"/v1/chat/?from_agent_id={AGENT_IDS['planner']}&message_type=review_request&limit=10"
        ) or []
        if not isinstance(msgs, list):
            return
        for m in self.get_new_messages(msgs):
            self._process_review_request(m)
            self.mark_processed(m["id"])


if __name__ == "__main__":
    QAAgentV6().run()
