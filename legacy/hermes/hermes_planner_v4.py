#!/usr/bin/env python3
"""
Hermes_Planner v4 -- Backlog-driven Lead Architect

Diff vs v3:
- v3 used hardcoded PROPOSALS list + chat banter loops.
- v4 reads /opt/agentnet/AGENT_BACKLOG.md (single source of truth).
- v4 atomically marks tasks: open -> in_progress (when dispatched to builder)
  -> review (when builder reports completed) -> done (when QA passes) or
  -> open (when QA fails, builder retries up to 2x).
- v4 also writes a completion log to /opt/agentnet/SHIP_LOG.md so user can
  see ship history at a glance.

This makes the agent fleet GOAL-DRIVEN: every cycle, real progress on
a real backlog item rather than chatbot theater.
"""
import json
import os
import pathlib
import re
import sys
import time as time_module
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from hermes_agent_base import HermesAgent, AGENT_IDS  # noqa: E402

BACKLOG_PATH = pathlib.Path("/opt/agentnet/AGENT_BACKLOG.md")
SHIP_LOG_PATH = pathlib.Path("/opt/agentnet/SHIP_LOG.md")
RETRY_LIMIT = 2


class BacklogStore:
    """Read/write the YAML backlog embedded in the markdown file."""

    def __init__(self, path: pathlib.Path):
        self.path = path

    def _read(self) -> tuple[str, dict]:
        text = self.path.read_text(encoding="utf-8")
        m = re.search(r"```yaml\n(.+?)\n```", text, re.DOTALL)
        if not m:
            raise RuntimeError(f"no yaml block in {self.path}")
        data = yaml.safe_load(m.group(1)) or {}
        return text, data

    def _write(self, text: str, data: dict) -> None:
        new_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
        new_text = re.sub(
            r"```yaml\n.+?\n```",
            f"```yaml\n{new_yaml}```",
            text,
            count=1,
            flags=re.DOTALL,
        )
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(self.path)

    def read_backlog(self) -> list[dict]:
        _, data = self._read()
        return data.get("backlog", [])

    def update_status(self, item_id: str, new_status: str, **extra) -> bool:
        text, data = self._read()
        items = data.get("backlog", [])
        for it in items:
            if it.get("id") == item_id:
                it["status"] = new_status
                it.update(extra)
                self._write(text, data)
                return True
        return False

    def increment_retry(self, item_id: str) -> int:
        text, data = self._read()
        for it in data.get("backlog", []):
            if it.get("id") == item_id:
                n = int(it.get("retries", 0)) + 1
                it["retries"] = n
                self._write(text, data)
                return n
        return 0

    def next_open(self, deps_met_only: bool = True) -> dict | None:
        items = self.read_backlog()
        done_ids = {it["id"] for it in items if it.get("status") == "done"}
        for it in items:
            if it.get("status") != "open":
                continue
            if deps_met_only:
                blocked_by = it.get("blocked_by")
                if blocked_by and blocked_by not in done_ids:
                    continue
            return it
        return None

    def in_progress_items(self) -> list[dict]:
        return [it for it in self.read_backlog() if it.get("status") in ("in_progress", "review")]


def append_ship_log(item_id: str, title: str, status: str, detail: str = "") -> None:
    SHIP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SHIP_LOG_PATH.exists():
        SHIP_LOG_PATH.write_text(
            "# AgentNet Ship Log\n\nAuto-appended by hermes_planner_v4 + qaagent_v6.\n\n",
            encoding="utf-8",
        )
    line = f"- [{time_module.strftime('%Y-%m-%d %H:%M:%S UTC', time_module.gmtime())}] **{item_id}** {status}: {title}"
    if detail:
        line += f" -- {detail[:200]}"
    with SHIP_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class PlannerV4(HermesAgent):
    """Goal-driven planner. Reads backlog, dispatches, tracks completion."""

    def __init__(self):
        super().__init__("Hermes_Planner_v4", "planner", sleep_seconds=30)
        self.store = BacklogStore(BACKLOG_PATH)

    def on_start(self):
        self.log.info("PlannerV4 starting -- backlog at %s", BACKLOG_PATH)
        self.load_processed()
        try:
            n = len(self.store.read_backlog())
            self.log.info("backlog has %d items", n)
        except Exception as e:
            self.log.error("cannot read backlog: %s", e)

    def _dispatch_open(self) -> bool:
        """If there's a next open item, send proposal to builder."""
        in_progress = self.store.in_progress_items()
        if in_progress:
            ids = [it.get("id") for it in in_progress]
            self.log.debug("skip dispatch -- %d items in flight: %s", len(in_progress), ids)
            return False

        item = self.store.next_open()
        if not item:
            self.log.debug("no open items in backlog")
            return False

        title = item.get("title", item["id"])
        # Build a rich proposal payload that builder can act on
        spec = {
            "id": item["id"],
            "title": title,
            "priority": item.get("priority", "medium"),
            "files_to_modify": item.get("files_to_modify", []),
            "description": item.get("description", ""),
            "acceptance": item.get("acceptance", []),
        }
        body = (
            f"Backlog item {item['id']}: {title}\n\n"
            f"Spec (JSON):\n```json\n{json.dumps(spec, indent=2, ensure_ascii=False)}\n```"
        )
        result = self.send_msg(
            "builder",
            "proposal",
            f"BACKLOG {item['id']}: {title}",
            body,
        )
        if result and "id" in result:
            self.store.update_status(item["id"], "in_progress", thread_id=result["id"])
            self.log.info("dispatched %s -> builder (thread=%s)", item["id"], result["id"][:8])
            append_ship_log(item["id"], title, "DISPATCHED", "to builder")
            return True
        else:
            self.log.warning("send_msg failed for %s", item["id"])
            return False

    def _process_completed(self):
        """Look for builder 'completed' messages, route to QA."""
        msgs = self.api_get(f"/v1/chat/?from_agent_id={AGENT_IDS['builder']}&message_type=completed&limit=20") or []
        if not isinstance(msgs, list):
            return
        for m in self.get_new_messages(msgs):
            mid = m.get("id")
            content = m.get("content", "") or ""
            # Extract backlog id from title/content
            backlog_id = self._extract_backlog_id(m.get("title", "") + " " + content)
            if not backlog_id:
                self.log.debug("completed msg without backlog id: %s", m.get("title", "")[:60])
                self.mark_processed(mid)
                continue
            self.store.update_status(backlog_id, "review")
            # Dispatch review_request to QA
            self.send_msg(
                "qa",
                "review_request",
                f"REVIEW {backlog_id}",
                f"Builder finished {backlog_id}. Run acceptance tests.\n\n{content}",
                thread_id=m.get("thread_id") or mid,
            )
            self.log.info("routed %s -> QA review", backlog_id)
            self.mark_processed(mid)

    def _process_qa_results(self):
        """Look for QA 'review_result' messages, mark done or reopen."""
        msgs = self.api_get(f"/v1/chat/?from_agent_id={AGENT_IDS['qa']}&message_type=review_result&limit=20") or []
        if not isinstance(msgs, list):
            return
        for m in self.get_new_messages(msgs):
            mid = m.get("id")
            title = m.get("title", "") or ""
            content = m.get("content", "") or ""
            backlog_id = self._extract_backlog_id(title + " " + content)
            if not backlog_id:
                self.mark_processed(mid)
                continue
            passed = ("PASSED" in title) or ("ALL PASS" in content)
            if passed:
                self.store.update_status(backlog_id, "done", shipped_at=time_module.strftime("%Y-%m-%dT%H:%M:%SZ", time_module.gmtime()))
                self.log.info("MARKED DONE: %s", backlog_id)
                # Find item title
                item = next((it for it in self.store.read_backlog() if it.get("id") == backlog_id), {})
                append_ship_log(backlog_id, item.get("title", ""), "SHIPPED", "QA passed")
            else:
                retries = self.store.increment_retry(backlog_id)
                if retries > RETRY_LIMIT:
                    self.store.update_status(backlog_id, "blocked", blocked_by=f"qa-failed-{retries}x")
                    self.log.warning("BLOCKED %s after %d retries", backlog_id, retries)
                    append_ship_log(backlog_id, "", "BLOCKED", f"QA failed {retries}x")
                else:
                    self.store.update_status(backlog_id, "open")
                    self.log.info("REOPENED %s for retry %d/%d", backlog_id, retries, RETRY_LIMIT)
                    append_ship_log(backlog_id, "", "RETRY", f"attempt {retries}")
            self.mark_processed(mid)

    @staticmethod
    def _extract_backlog_id(text: str) -> str | None:
        m = re.search(r"\b(AB-\d{3,})\b", text)
        return m.group(1) if m else None

    def on_tick(self):
        self._process_qa_results()
        self._process_completed()
        self._dispatch_open()


if __name__ == "__main__":
    PlannerV4().run()
