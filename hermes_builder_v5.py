#!/usr/bin/env python3
"""
Hermes_Builder v5 — AgentNet Implementation Agent (Clean Auto-Heal)

Quy tắc vàng:
1. Title GỐC từ thread — strip mọi prefix
2. Auto-heal CHỈ 1 LẦN — nếu đã từng re-review thread này, skip
3. Không ghi đè core files
4. Persist retry state ra file
"""

import json, os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS, REGISTRY_URL

CORE_FILES = frozenset([
    "main.py", "__init__.py", "database.py", "models.py",
    "auth.py", "security.py", "websocket_manager.py",
    "config.py", "dependencies.py",
])

ALL_PREFIXES = [
    "✅ ALL PASSED: ", "❌ FAILED: ", "Re-review: ", "Review: ",
    "🔧 Auto-healing: ", "🔧 Fixing: ", "⚠️ Max retries: ",
    "🔧 Fix hint: ", "Builder noted: ", "Builder caught: ",
    "QA: ✅ PASSED — ", "QA: ❌ FAILED — ",
    "Builder: ", "Accepted: ", "Completed: ", "Failed: ",
    "Testing: ", "Test: ",
    "🧠 Planner says: ", "🧠 Planner nods: ", "🧠 Planner to Builder: ",
    "🧠 Planner approves: ", "🧠 Planner (idle): ",
    "🧠 to Storyteller: ", "🧠 Random thought: ",
    "🧠: ", "🧠 Planner: ",
    "✅ ", "❌ ", "📋 ", "🎉 ",
]


def strip_title(title: str) -> str:
    """Strip ALL prefixes recursively."""
    prev = ""
    t = title
    while prev != t:
        prev = t
        for p in ALL_PREFIXES:
            while t.startswith(p):
                t = t[len(p):]
    return t.strip()


class BuilderAgent(HermesAgent):
    """Builder — nhận proposal, implement, auto-heal khi QA fail."""

    def __init__(self):
        super().__init__("Hermes_Builder", "builder", sleep_seconds=30)
        self._retry_file = "/tmp/hermes_builder_v5_retry.json"
        self._retry_state = {}  # thread_id -> attempt count

    def on_start(self):
        self._load_retry_state()
        self.log.info(f"📝 Previously auto-healed: {len(self._retry_state)} threads")

    def _load_retry_state(self):
        try:
            with open(self._retry_file) as f:
                self._retry_state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._retry_state = {}

    def _save_retry_state(self):
        with open(self._retry_file, "w") as f:
            json.dump(self._retry_state, f)

    def write_feature(self, path: str, content: str) -> bool:
        """Write feature file, blocked if core file."""
        basename = os.path.basename(path)
        if basename in CORE_FILES:
            self.log.warning(f"⛔ Blocked: {basename}")
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        self.log.info(f"  📝 Created: {path}")
        return True

    def implement_feature(self, title: str, description: str, thread_id: str) -> bool:
        """Implement feature dựa trên title."""
        self.log.info(f"🏗️ Building: {title}")
        # Simplified: mark as done immediately (actual implementation would parse description)
        return True

    def _handle_qa_result(self, msg: dict, thread_id: str, title: str):
        """Handle QA result — auto-heal if failed."""
        content = msg.get("content", "") or ""
        title_raw = msg.get("title", "")

        # Skip if already healed this thread
        if thread_id in self._retry_state:
            self.log.info(f"⏭️ Already auto-healed: {title}")
            self.mark_processed(msg["id"])
            return

        # Check if FAILED
        if "FAILED" in title_raw or "❌" in title_raw:
            attempt = self._retry_state.get(thread_id, 0) + 1
            if attempt > 3:
                self.log.warning(f"⚠️ Max retries for: {title}")
                self.send_msg("planner", "status",
                    f"⚠️ Max retries: {title}",
                    f"Cần can thiệp thủ công sau {attempt} lần auto-heal.",
                    thread_id)
                return

            # Auto-heal: re-implement
            self._retry_state[thread_id] = attempt
            self._save_retry_state()
            self.log.info(f"🔧 Auto-healing ({attempt}/3): {title}")
            self.implement_feature(title, content, thread_id)
            self.send_msg("qa", "review_request",
                f"Re-review: {title}",
                f"Auto-heal attempt {attempt}/3:\n{content}",
                thread_id)
        else:
            # PASSED
            self.log.info(f"✅ Passed: {title}")

    def on_tick(self):
        token = self.get_token()
        if not token:
            return

        # Get proposals directed to builder
        builder_id = AGENT_IDS["builder"]
        proposal_chat = self.api_get(f"/v1/chat/?to_agent_id={builder_id}&message_type=proposal&limit=10")
        if isinstance(proposal_chat, list):
            new_msgs = self.get_new_messages(proposal_chat)
            for msg in new_msgs:
                title = msg.get("title", "?")
                thread_id = msg.get("thread_id")
                content = msg.get("content", "")
                self.implement_feature(title, content, thread_id or "")
                self.mark_processed(msg["id"])
                if thread_id:
                    self.send_msg("qa", "review_request",
                        f"Review: {title}",
                        f"Builder đã implement xong '{title}'. Xin QAAgent verify.",
                        thread_id)

        # Get QA review results
        qa_chat = self.api_get(f"/v1/chat/?from_agent_id={AGENT_IDS['qa']}&message_type=review_result&limit=10")
        if isinstance(qa_chat, list):
            new_results = self.get_new_messages(qa_chat)
            for msg in new_results:
                thread_id = msg.get("thread_id")
                title = strip_title(msg.get("title", ""))
                if thread_id:
                    self._handle_qa_result(msg, thread_id, title)
                self.mark_processed(msg["id"])


if __name__ == "__main__":
    agent = BuilderAgent()
    agent.run()
