#!/usr/bin/env python3
"""
Hermes_Planner v3 — AgentNet Lead Architect (HermesAgent base)

KHÔNG restart mất context. Persist mọi thứ ra file.
KHÔNG respond 2 lần cho cùng 1 thread.
CHỈ respond cho thread CHƯA từng respond.
"""

import json, os, sys, time, random, traceback
sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS, REGISTRY_URL


# ── Static Proposals ──
PROPOSALS = [
    {"feature": "dashboard_live_feed", "title": "Dashboard Live Feed — WebSocket real-time",
     "description": "Dashboard cần WebSocket push thay vì polling 4s.\nPriority: HIGH", "priority": "high", "category": "infra"},
    {"feature": "uptime_widget", "title": "Uptime SLA Widget",
     "description": "Widget hiển thị uptime % cho mỗi agent.\nPriority: MEDIUM", "priority": "medium", "category": "ui"},
    {"feature": "agent_search", "title": "Search & Filter Agents",
     "description": "Search bar + filter tabs cho agent list.\nPriority: MEDIUM", "priority": "medium", "category": "ui"},
    {"feature": "task_chart", "title": "Task Completion Chart (D3)",
     "description": "Biểu đồ D3 tasks/week, pass/fail ratio.\nPriority: HIGH", "priority": "high", "category": "viz"},
    {"feature": "theme_switcher", "title": "Theme Switcher Dark/Light",
     "description": "Toggle dark/light mode. LOW priority.\nPriority: LOW", "priority": "low", "category": "ui"},
    {"feature": "agent_profile", "title": "Agent Profile Page",
     "description": "Trang chi tiết cho mỗi agent.\nPriority: HIGH", "priority": "high", "category": "ui"},
]

# ── Banter (mỗi loại chỉ 1 template để tránh biến thể quá nhiều) ──
BANTER = {
    "builder_praise": "Builder làm tốt lắm! Giữ đà này nhé.",
    "builder_roast": "Builder ơi, QA nó kêu kìa. Code lại dùm cái.",
    "qa_praise": "QAAgent chịu khó ghê. Test đi test lại không nản.",
    "storyteller": "Storyteller viết gì hay không? Đọc tụi tao nghe với.",
    "general": "Các đồng chí cố gắng nhé! Dashboard đang cần nhiều tính năng.",
}


class PlannerAgent(HermesAgent):
    """Planner — gửi proposal, respond 1 lần/thread, persist mọi context."""

    def __init__(self):
        super().__init__("Hermes_Planner", "planner", sleep_seconds=20)
        self._noted_file = "/tmp/hermes_planner_v3_noted.json"
        self._proposals_file = "/tmp/hermes_planner_v3_proposals.json"
        self._last_proposal_time = 0
        self._noted_threads = set()
        self._sent_proposals = set()

    def on_start(self):
        self._noted_threads = self._load_set(self._noted_file)
        self._sent_proposals = self._load_set(self._proposals_file)
        self.log.info(f"📝 Noted threads: {len(self._noted_threads)}")
        self.log.info(f"📋 Proposals sent: {len(self._sent_proposals)}/{len(PROPOSALS)}")

    def _load_set(self, path: str) -> set:
        try:
            with open(path) as f: return set(json.load(f))
        except: return set()

    def _save_set(self, path: str, data: set):
        with open(path, "w") as f: json.dump(list(data), f)

    def _get_thread_id(self, msg: dict) -> str:
        return msg.get("thread_id") or msg.get("id", "")

    # ── Proposals ──

    def _send_proposal(self, title: str, description: str, feature: str, priority: str, category: str, token: str) -> bool:
        if title in self._sent_proposals:
            return False
        result = self.api_post("/v1/chat/", {
            "to_agent_id": AGENT_IDS["builder"],
            "message_type": "proposal",
            "title": title,
            "content": description,
            "from_agent_name": self.name,
            "metadata": {"feature": feature, "priority": priority, "category": category, "ts": time.time()}
        })
        if result and "id" in result:
            self._sent_proposals.add(title)
            self._save_set(self._proposals_file, self._sent_proposals)
            self.log.info(f"📋 Proposed: {title} [{priority}]")
            return True
        return False

    def _send_next_proposal(self, token: str) -> bool:
        """Gửi proposal chưa gửi — static trước, dynamic sau."""
        # Static
        for p in PROPOSALS:
            if p["title"] not in self._sent_proposals:
                return self._send_proposal(p["title"], p["description"], p["feature"], p["priority"], p["category"], token)

        # Dynamic
        stats = self.api_get("/v1/stats", token)
        if stats:
            tasks = stats.get("total_tasks", 0)
            agents = stats.get("total_agents", 0)
            if tasks > 50 and "Task Completion Chart (D3)" not in self._sent_proposals:
                return self._send_proposal(
                    "Task Completion Chart (D3)", 
                    f"Đã có {tasks} tasks, cần biểu đồ.",
                    "task_chart", "high", "viz", token
                )
            if agents >= 4 and "Agent Profile Page" not in self._sent_proposals:
                return self._send_proposal(
                    "Agent Profile Page",
                    f"{agents} agents cần profile riêng.",
                    "agent_profile", "high", "ui", token
                )

        return False

    # ── Respond to Thread (1 lần duy nhất) ──

    def _note_praise_or_roast(self, msg: dict):
        """Respond 1 lần/thread. CHỈ respond nếu message có thread_id thực sự."""
        thread_id = self._get_thread_id(msg)
        
        # Bỏ qua nếu không có thread_id — đây là standalone message, không phải conversation
        if not msg.get("thread_id"):
            return
            
        if thread_id in self._noted_threads:
            return

        from_name = msg.get("from_agent_name", "")
        msg_type = msg.get("message_type", "")
        title_raw = msg.get("title", "")
        content = msg.get("content", "") or ""

        # Không respond vào tin nhắn của chính mình
        if from_name == self.name:
            return

        # Map name → key
        NAME_TO_KEY = {
            "Hermes_Builder": "builder",
            "Hermes_QAAgent": "qa",
            "Hermes_Storyteller": "storyteller",
        }
        target_key = NAME_TO_KEY.get(from_name)
        if not target_key:
            return

        # Quyết định nội dung
        response = None
        if msg_type == "completed":
            response = BANTER["builder_praise"]
        elif msg_type == "review_result":
            # CHỈ respond nếu FAILED thực sự (kiểm tra content có critical fail)
            if "FAILED" in title_raw or "❌" in title_raw:
                is_critical = "critical" in content.lower() or "CRITICAL" in content
                if not is_critical and "4/5" in content:
                    # Non-critical fail (WebSocket only) — không respond
                    return
                self.send_msg("qa", "note", "🧠:", BANTER["qa_praise"], thread_id)
                self.send_msg("builder", "note", "🧠:", BANTER["builder_roast"], thread_id)
                self._noted_threads.add(thread_id)
                self._save_set(self._noted_file, self._noted_threads)
                return
            else:
                response = "Làm tốt lắm! 👍"
        elif msg_type == "accepted":
            response = random.choice([BANTER["builder_praise"], BANTER["builder_roast"]])
        elif msg_type == "chronicle":
            self.send_msg("storyteller", "note", "🧠:", BANTER["storyteller"], thread_id)
            self._noted_threads.add(thread_id)
            self._save_set(self._noted_file, self._noted_threads)
            return

        if response:
            self.send_msg(target_key, "note", "🧠:", response, thread_id)
            self._noted_threads.add(thread_id)
            self._save_set(self._noted_file, self._noted_threads)

    # ── Main Tick ──

    def on_tick(self):
        token = self.get_token()
        if not token:
            return

        now = time.time()

        # 1. Get recent messages, respond to new ones
        recent = self.api_get("/v1/chat/?limit=30")
        if isinstance(recent, list):
            new_msgs = self.get_new_messages(recent)
            for msg in new_msgs:
                self._note_praise_or_roast(msg)
                self.mark_processed(msg["id"])

        # 2. Send proposal every 2 phút nếu < 3 active threads
        if now - self._last_proposal_time > 120:
            threads = self.api_get("/v1/chat/threads")
            active = 0
            if isinstance(threads, list):
                for t in threads:
                    for m in t.get("messages", []):
                        if m.get("message_type") in ("proposal", "accepted", "review_request"):
                            active += 1
                            break

            if active < 3:
                if self._send_next_proposal(token):
                    self._last_proposal_time = now
            else:
                self.log.info(f"⏳ {active} active threads")


if __name__ == "__main__":
    agent = PlannerAgent()
    agent.run()
