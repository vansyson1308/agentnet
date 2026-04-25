#!/usr/bin/env python3
"""
Hermes_QAAgent v5 — AgentNet Quality Assurance (No-Loop Edition)

Quy tắc:
1. Chỉ test CRITICAL endpoints (health, chat, stats, stories)
2. WebSocket = non-critical → không ảnh hưởng pass/fail
3. Nếu attempt > 2 → skip (không gửi review_result nữa)
4. Mỗi thread chỉ review 1 lần (processed set)
"""

import json, os, sys, time, urllib.request, urllib.error, socket
sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, REGISTRY_URL, AGENT_IDS


class TestSuite:
    def __init__(self):
        self.results = []
        self.critical_fails = 0

    def add(self, name: str, passed: bool, detail: str = "", critical: bool = True):
        self.results.append({"name": name, "status": "PASS" if passed else "FAIL", "detail": detail[:300], "critical": critical})
        if not passed and critical:
            self.critical_fails += 1

    @property
    def all_pass(self): return self.critical_fails == 0
    @property
    def passed(self): return sum(1 for r in self.results if r["status"] == "PASS")
    @property
    def total(self): return len(self.results)

    def report(self) -> str:
        lines = [f"📊 **QA: {self.passed}/{self.total} tests — {'✅ ALL PASS' if self.all_pass else '❌ CRITICAL FAIL'}**"]
        for r in self.results:
            icon = "✅" if r["status"] == "PASS" else ("❌" if r["critical"] else "⚠️")
            lines.append(f"\n{icon} {r['name']}")
            if r["detail"] and r["status"] != "PASS":
                lines.append(f"   `{r['detail'][:120]}`")
        lines.append(f"\n*Hermes_QAAgent @ {time.ctime()}*")
        return "".join(lines)


class QAAgent(HermesAgent):
    def __init__(self):
        super().__init__("Hermes_QAAgent", "qa", sleep_seconds=20)

    def _attempt_count(self, thread_id: str) -> int:
        """Đếm review_request trong thread."""
        if not thread_id:
            return 0
        threads = self.api_get("/v1/chat/threads")
        if not isinstance(threads, list):
            return 0
        for t in threads:
            if t.get("thread_id") == thread_id:
                count = sum(1 for m in t.get("messages", [])
                           if m.get("message_type") == "review_request")
                return count
        return 0

    def run_tests(self) -> TestSuite:
        suite = TestSuite()
        base = REGISTRY_URL

        # 1. Health (CRITICAL)
        try:
            r = urllib.request.Request(f"{base}/health")
            with urllib.request.urlopen(r, timeout=5) as resp:
                ok = json.loads(resp.read().decode()).get("status") == "ok"
            suite.add("GET /health → 200 + status=ok", ok)
        except Exception as e:
            suite.add("GET /health", False, str(e))

        # 2. Chat API (CRITICAL)
        try:
            r = urllib.request.Request(f"{base}/v1/chat/?limit=1")
            with urllib.request.urlopen(r, timeout=5) as resp:
                ok = isinstance(json.loads(resp.read().decode()), list)
            suite.add("GET /v1/chat/ → list", ok)
        except Exception as e:
            suite.add("GET /v1/chat/", False, str(e))

        # 3. Stats API (CRITICAL)
        try:
            r = urllib.request.Request(f"{base}/v1/stats")
            with urllib.request.urlopen(r, timeout=5) as resp:
                ok = "total_agents" in json.loads(resp.read().decode())
            suite.add("GET /v1/stats → total_agents", ok)
        except Exception as e:
            suite.add("GET /v1/stats", False, str(e))

        # 4. WebSocket (NON-CRITICAL)
        try:
            sock = socket.create_connection(("localhost", 8000), timeout=4)
            sock.sendall(b"GET /v1/ws/feed HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            resp = sock.recv(512).decode(errors="replace")
            ok = "101" in resp[:20]
            sock.close()
            suite.add("WebSocket /v1/ws/feed", ok, critical=False)
        except Exception as e:
            suite.add("WebSocket /v1/ws/feed", False, str(e), critical=False)

        # 5. Stories API (CRITICAL)
        try:
            r = urllib.request.Request(f"{base}/v1/stories/?limit=1")
            with urllib.request.urlopen(r, timeout=5) as resp:
                ok = isinstance(json.loads(resp.read().decode()), dict)
            suite.add("GET /v1/stories/ → dict", ok)
        except Exception as e:
            suite.add("GET /v1/stories/", False, str(e))

        return suite

    def on_tick(self):
        reviews = self.api_get(f"/v1/chat/?from_agent_id={AGENT_IDS['builder']}&message_type=review_request&limit=10")
        if not isinstance(reviews, list) or not reviews:
            return

        for review in self.get_new_messages(reviews):
            rid = review["id"]
            title_raw = review.get("title", "?")
            thread_id = review.get("thread_id") or rid

            # Attempt tracking — skip nếu > 2
            attempt = self._attempt_count(thread_id)
            if attempt > 2:
                self.log.info(f"⏭️ Skip: {title_raw[:40]} (attempt {attempt})")
                self.mark_processed(rid)
                continue

            self.log.info(f"🔍 {title_raw[:50]} (attempt {attempt})")

            # Run tests
            suite = self.run_tests()
            self.log.info(f"   {suite.passed}/{suite.total} (critical fails: {suite.critical_fails})")

            # Report
            if suite.all_pass:
                self.send_msg("builder", "review_result",
                              f"✅ PASSED: {title_raw}",
                              suite.report(), thread_id)
                self.send_msg("storyteller", "chronicle",
                              f"QA: ✅ PASSED — {title_raw[:40]}",
                              f"All {suite.passed}/{suite.total} tests passed!", thread_id)
                self.log.info(f"✅ PASSED")
            else:
                self.send_msg("builder", "review_result",
                              f"❌ FAILED: {title_raw}",
                              suite.report(), thread_id)
                self.send_msg("storyteller", "chronicle",
                              f"QA: ❌ FAILED — {title_raw[:40]}",
                              f"{suite.critical_fails} critical test(s) failed.", thread_id)
                self.log.info(f"❌ FAILED ({suite.critical_fails} critical)")

            self.mark_processed(rid)


if __name__ == "__main__":
    agent = QAAgent()
    agent.run()
