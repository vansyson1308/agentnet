#!/usr/bin/env python3
"""
Hermes Stuck-Issue Monitor with Telegram Alert
- Generates Paperclip metrics (delegates to paperclip_metrics.py)
- Checks for stuck issues (>30 min in_progress)
- Sends Telegram alert via Hermes send_message if stuck issues found
- Keeps alert state to avoid duplicate notifications
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

METRICS_JSON = "/tmp/paperclip_metrics.json"
ALERT_STATE = "/tmp/hermes_stuck_alert_state.json"
STUCK_THRESHOLD_MIN = 30


def load_alert_state() -> dict:
    if os.path.exists(ALERT_STATE):
        try:
            with open(ALERT_STATE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alerted": {}}


def save_alert_state(state: dict):
    with open(ALERT_STATE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    # ── Step 1: Generate metrics ──
    script = os.path.join(os.path.dirname(__file__), "paperclip_metrics.py")
    result = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, timeout=30,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if not os.path.exists(METRICS_JSON):
        print("[alert] No metrics JSON, skipping alert check")
        return

    with open(METRICS_JSON) as f:
        metrics = json.load(f)

    stuck = metrics.get("stuck_issues", [])
    if not stuck:
        print("[alert] No stuck issues — all clear ✅")
        # Clear state
        save_alert_state({"last_alerted": {}})
        return

    state = load_alert_state()
    last_alerted = state.get("last_alerted", {})
    now = datetime.now(timezone.utc)

    new_stuck = []
    for s in stuck:
        issue_id = s["id"]
        # Only alert if not alerted in last 2 hours
        last_time = last_alerted.get(issue_id, 0)
        if now.timestamp() - last_time > 7200:  # 2h cooldown
            new_stuck.append(s)
            last_alerted[issue_id] = now.timestamp()

    if new_stuck:
        # Build alert message
        lines = [
            "⚠️ **AgentNet Stuck Issue Alert**",
            "",
            f"{len(new_stuck)} issue(s) stuck > {STUCK_THRESHOLD_MIN} min:",
            "",
        ]
        for s in new_stuck:
            lines.append(
                f"• **{s['identifier']}** — {s['title'][:60]}"
            )
            lines.append(f"  Stuck for {s['elapsed_min']} min")
            lines.append("")

        lines.append("_Check: http://localhost:8080/metrics_")
        lines.append(f"_{now.strftime('%Y-%m-%d %H:%M UTC')}_")

        message = "\n".join(lines)
        print(f"[alert] {len(new_stuck)} new stuck issues detected!")
        print(message)

        # Write alert to file for cron delivery
        alert_file = "/tmp/hermes_stuck_alert.txt"
        with open(alert_file, "w") as f:
            f.write(message)
        print(f"[alert] Alert written to {alert_file}")

        save_alert_state({"last_alerted": last_alerted})
    else:
        print(f"[alert] {len(stuck)} stuck issues but all already alerted (cooldown active)")


if __name__ == "__main__":
    main()
