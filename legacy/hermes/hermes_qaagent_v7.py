#!/usr/bin/env python3
"""
Hermes_QAAgent v7 -- QA Agent with Paperclip Feedback Loop

New in v7:
- Paperclip integration: auto-update issue status after QA run
  - ALL PASS → PATCH /api/issues/:id { status: "done" }
  - ANY FAIL → PATCH /api/issues/:id { status: "blocked", comment: "..." }
- Parses both legacy AB-XXX and Paperclip PAP-N / UUID identifiers
- Fallback: if Paperclip unreachable, still sends chat result (graceful degradation)
- Maps AB-XXX → Paperclip issue by searching Paperclip API by title

Legacy (v6):
- Reads acceptance criteria from AGENT_BACKLOG.md YAML
- Runs each criterion as bash -c, reports PASS/FAIL
- Sends review_result via AgentNet chat

Paperclip API:
- PATCH http://localhost:3100/api/issues/:id { status: "done"|"blocked", comment?: "..." }
- GET http://localhost:3100/api/companies/:cid/issues?status=in_progress (for mapping)
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time as time_module
import urllib.request
import urllib.parse
import uuid
import yaml
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS  # noqa: E402

REPO_ROOT = pathlib.Path("/opt/agentnet")
BACKLOG_PATH = REPO_ROOT / "AGENT_BACKLOG.md"
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"

# ── Paperclip config ──────────────────────────────────────────────
PAPERCLIP_BASE = os.environ.get("PAPERCLIP_URL", "http://localhost:3100")
PAPERCLIP_COMPANY_ID = os.environ.get(
    "PAPERCLIP_COMPANY_ID",
    "bbb50bef-ce01-4cc8-aac9-e33dae6395c0",
)


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


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
    """Auth token for AgentNet registry — uses base class token cache."""
    # Delegate to HermesAgent's built-in token management
    import importlib
    try:
        # Use the QA instance's get_token if available (called from class method)
        return _cached_token
    except NameError:
        pass
    # Fallback: direct login
    import urllib.request
    data = urllib.parse.urlencode({
        "username": "annhien.dev@gmail.com",
        "password": "TestPass123",
    }).encode()
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


# ── Paperclip helpers ─────────────────────────────────────────────


def _paperclip_patch(issue_id: str, payload: dict) -> bool:
    """PATCH a Paperclip issue. Returns True on success."""
    url = f"{PAPERCLIP_BASE}/api/issues/{issue_id}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        print(f"[paperclip] PATCH {issue_id} → HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    except Exception as e:
        print(f"[paperclip] PATCH {issue_id} → error: {e}")
        return False


def _paperclip_find_issue_by_title(
    title_substring: str, status: str = "in_progress"
) -> Optional[str]:
    """Search Paperclip issues by title substring, return issue UUID or None."""
    url = (
        f"{PAPERCLIP_BASE}/api/companies/{PAPERCLIP_COMPANY_ID}"
        f"/issues?status={status}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            issues = json.loads(r.read().decode())
    except Exception as e:
        print(f"[paperclip] search issues error: {e}")
        return None

    lower = title_substring.lower()
    # Fuzzy: try exact match first, then substring
    for issue in issues:
        if title_substring.lower() in issue.get("title", "").lower():
            return issue["id"]
    return None


def _extract_paperclip_issue_id(content: str, title: str) -> Optional[str]:
    """Try to extract a Paperclip issue UUID from review_request content/title."""
    text = f"{title} {content}"

    # 1. Direct UUID in text
    uuid_matches = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        text, re.IGNORECASE,
    )
    for m in uuid_matches:
        # Verify it's a real issue by pinging Paperclip
        try:
            url = f"{PAPERCLIP_BASE}/api/issues/{m}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status < 300:
                    return m
        except Exception:
            continue

    # 2. PAP-N format → search by identifier
    pap_match = re.search(r"\b(PAP-\d+)\b", text)
    if pap_match:
        identifier = pap_match.group(1)
        url = (
            f"{PAPERCLIP_BASE}/api/companies/{PAPERCLIP_COMPANY_ID}"
            f"/issues?status=in_progress"
        )
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                issues = json.loads(r.read().decode())
            for issue in issues:
                if issue.get("identifier") == identifier:
                    return issue["id"]
        except Exception:
            pass

    return None


# ── QAAgent v7 ────────────────────────────────────────────────────


class QAAgentV7(HermesAgent):
    def __init__(self):
        super().__init__("Hermes_QAAgent_v7", "qa", sleep_seconds=20)
        self._restart_attempted_for = set()
        self._paperclip_unreachable_since: float = 0

    def on_start(self):
        self.load_processed()
        self.log.info(
            "QAAgent_v7 starting — backlog=%s, paperclip=%s, company=%s",
            BACKLOG_PATH, PAPERCLIP_BASE, PAPERCLIP_COMPANY_ID,
        )

    def _maybe_restart_services(self, item_id: str, files_modified: list[str]) -> None:
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
            time_module.sleep(8)
        except Exception as e:
            self.log.warning("restart failed: %s", e)

    def _run_acceptance(self, criterion: str, env: dict[str, str]) -> tuple[bool, str]:
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

        # ── Step 1: Extract backlog ID ──
        # Try AB-XXX first (legacy YAML), then Paperclip UUID/PAP-N
        m_yaml = re.search(r"\b(AB-\d{3,})\b", title + " " + content)
        paperclip_issue_id = _extract_paperclip_issue_id(content, title)

        if not m_yaml and not paperclip_issue_id:
            self.log.warning("review_request without identifiable issue: %s", title[:80])
            return

        backlog_id = m_yaml.group(1) if m_yaml else (paperclip_issue_id or "UNKNOWN")
        item = None

        if m_yaml:
            # Legacy: load from YAML
            item = _load_backlog_item(backlog_id)
            if item is None:
                self.log.warning("backlog item %s not found in YAML, trying Paperclip", backlog_id)

        # ── Step 2: If no YAML item (or YAML item has no acceptance), use generic tests ──
        if item is None:
            item = {
                "id": backlog_id,
                "acceptance": [
                    f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8000/health | grep -q '^200$'",
                    f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8000/v1/agents/public/ | grep -q '^200$'",
                ],
                "files_to_modify": [],
                "title": title,
            }

        criteria = item.get("acceptance", [])
        files_modified = item.get("files_to_modify", [])
        item_title = item.get("title", backlog_id)

        if not criteria:
            self.log.warning("no acceptance criteria for %s", backlog_id)
            self._send_review_result(
                backlog_id, "No acceptance criteria defined", False,
                msg.get("thread_id") or msg.get("id"),
                paperclip_issue_id=paperclip_issue_id,
            )
            return

        # Restart services if needed
        if any("curl" in c for c in criteria):
            self._maybe_restart_services(backlog_id, files_modified)

        # Build env for substitution
        token = _get_token()
        agents_map = _list_agent_ids()
        env = {"TOKEN": token}
        if "hermes-brain" in agents_map:
            env["AGENT_ID"] = agents_map["hermes-brain"]

        # ── Step 3: Run tests ──
        results: list[tuple[str, bool, str]] = []
        for c in criteria:
            ok, out = self._run_acceptance(c, env)
            results.append((c, ok, out))
            self.log.info("  [%s] %s", "PASS" if ok else "FAIL", c[:80])

        passed = all(r[1] for r in results) and len(results) > 0

        # ── Step 4: Report ──
        report_lines = [
            f"QA verdict for {backlog_id}: {'✅ PASSED' if passed else '❌ FAILED'}",
            f"Tested {len(results)} acceptance criteria.",
            "",
        ]
        for c, ok, out in results:
            mark = "✅" if ok else "❌"
            report_lines.append(f"[{mark}] {c}")
            if not ok and out:
                report_lines.append(f"   output: {out[:200]}")
            if not ok:
                report_lines.append("")

        report_body = "\n".join(report_lines)

        # ── Step 5: Update Paperclip ──
        new_status = "done" if passed else "blocked"
        paperclip_ok = False

        if paperclip_issue_id:
            payload: dict = {"status": new_status}
            if not passed:
                # Add comment with failure details
                fail_summary = "\n".join(
                    f"- {c[:120]}" for c, ok, _ in results if not ok
                )
                payload["comment"] = f"QA failed: {fail_summary}"

            paperclip_ok = _paperclip_patch(paperclip_issue_id, payload)
            if paperclip_ok:
                self.log.info(
                    "📋 Paperclip %s → %s", paperclip_issue_id[:8], new_status,
                )
                report_lines.insert(
                    1, f"📋 Paperclip: {paperclip_issue_id[:8]}... → *{new_status}*",
                )
                report_body = "\n".join(report_lines)
            else:
                self.log.warning("Paperclip update failed for %s", paperclip_issue_id)
                report_lines.insert(
                    1, "⚠️ Paperclip update FAILED (API unreachable)",
                )
                report_body = "\n".join(report_lines)
        else:
            # Try to find by AB-XXX → search Paperclip by title
            if m_yaml and item_title:
                found_id = _paperclip_find_issue_by_title(item_title)
                if found_id:
                    self.log.info("AB-XXX mapped to Paperclip %s", found_id[:8])
                    payload = {"status": new_status}
                    if not passed:
                        fail_summary = "\n".join(
                            f"- {c[:120]}" for c, ok, _ in results if not ok
                        )
                        payload["comment"] = f"QA failed: {fail_summary}"
                    paperclip_ok = _paperclip_patch(found_id, payload)
                    if paperclip_ok:
                        report_lines.insert(
                            1, f"📋 Paperclip (auto-mapped): {found_id[:8]}... → *{new_status}*",
                        )
                        report_body = "\n".join(report_lines)

        self._send_review_result(
            backlog_id, report_body, passed,
            msg.get("thread_id") or msg.get("id"),
        )

    def _send_review_result(
        self,
        backlog_id: str,
        body: str,
        passed: bool,
        thread_id: str,
        paperclip_issue_id: Optional[str] = None,
    ) -> None:
        status_emoji = "✅" if passed else "❌"
        pc_tag = f" [Paperclip: {paperclip_issue_id[:8]}...]" if paperclip_issue_id else ""
        title = f"QA {status_emoji} {backlog_id}{pc_tag}"
        self.send_msg("planner", "review_result", title, body, thread_id=thread_id)
        self.log.info("sent review_result %s -> planner", title)

    def on_tick(self):
        msgs = (
            self.api_get(
                f"/v1/chat/?from_agent_id={AGENT_IDS['builder']}"
                f"&message_type=review_request&limit=10"
            )
            or []
        )
        if not isinstance(msgs, list):
            return
        for m in self.get_new_messages(msgs):
            self._process_review_request(m)
            self.mark_processed(m["id"])


if __name__ == "__main__":
    QAAgentV7().run()
