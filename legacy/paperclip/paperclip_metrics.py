#!/usr/bin/env python3
"""
Paperclip Metrics Generator
Cron job: fetch Paperclip issues, compute metrics, generate static JSON + HTML.
Output: /tmp/paperclip_metrics.json + /opt/agentnet/services/dashboard/app/templates/metrics.html
"""
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Optional

PAPERCLIP_BASE = os.environ.get("PAPERCLIP_URL", "http://localhost:3100")
COMPANY_ID = "bbb50bef-ce01-4cc8-aac9-e33dae6395c0"
OUTPUT_HTML = "/opt/agentnet/services/dashboard/app/templates/metrics.html"
OUTPUT_JSON = "/tmp/paperclip_metrics.json"


def fetch_json(url: str) -> Optional[list]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[metrics] fetch error {url}: {e}")
        return None


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    issues = fetch_json(
        f"{PAPERCLIP_BASE}/api/companies/{COMPANY_ID}/issues"
    )
    if not issues:
        print("[metrics] no issues retrieved, keeping existing data")
        return

    now = datetime.now(timezone.utc)

    # ── Compute metrics ──
    by_status = {}
    cycle_times: list[float] = []
    completed_count = 0
    failed_count = 0
    in_progress_count = 0
    total_issues = len(issues)

    for issue in issues:
        status = issue.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1

        if status in ("done", "blocked", "cancelled"):
            created = parse_iso(issue.get("createdAt"))
            completed = parse_iso(issue.get("completedAt")) or parse_iso(issue.get("updatedAt"))

            if created and completed:
                hours = (completed - created).total_seconds() / 3600
                cycle_times.append(hours)

            if status == "done":
                completed_count += 1
            elif status == "blocked":
                failed_count += 1
            elif status == "cancelled":
                pass
        elif status == "in_progress":
            in_progress_count += 1

    avg_cycle_hours = sum(cycle_times) / len(cycle_times) if cycle_times else 0
    max_cycle_hours = max(cycle_times) if cycle_times else 0
    min_cycle_hours = min(cycle_times) if cycle_times else 0

    total_closed = completed_count + failed_count
    success_rate = (completed_count / total_closed * 100) if total_closed > 0 else 0

    # ── Stuck issues (>30 min in_progress) ──
    stuck_issues = []
    for issue in issues:
        if issue.get("status") == "in_progress":
            started = parse_iso(issue.get("startedAt")) or parse_iso(issue.get("createdAt"))
            if started:
                elapsed_min = (now - started).total_seconds() / 60
                if elapsed_min > 30:
                    stuck_issues.append({
                        "id": issue["id"][:8],
                        "identifier": issue.get("identifier", "N/A"),
                        "title": issue.get("title", "")[:80],
                        "elapsed_min": round(elapsed_min, 1),
                    })

    # ── Per-agent stats (from issues with assigneeUserId) ──
    agent_stats: dict[str, dict] = {}
    for issue in issues:
        agent = issue.get("assigneeUserId") or "unassigned"
        if agent not in agent_stats:
            agent_stats[agent] = {"total": 0, "done": 0, "blocked": 0, "in_progress": 0}
        agent_stats[agent]["total"] += 1
        status = issue.get("status")
        if status in agent_stats[agent]:
            agent_stats[agent][status] += 1

    # ── Build output ──
    metrics = {
        "generated_at": now.isoformat(),
        "total_issues": total_issues,
        "by_status": by_status,
        "completed": completed_count,
        "failed": failed_count,
        "in_progress": in_progress_count,
        "success_rate_pct": round(success_rate, 1),
        "avg_cycle_hours": round(avg_cycle_hours, 2),
        "max_cycle_hours": round(max_cycle_hours, 2),
        "min_cycle_hours": round(min_cycle_hours, 2),
        "stuck_issues": stuck_issues,
        "agent_stats": agent_stats,
    }

    # Write JSON
    with open(OUTPUT_JSON, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"[metrics] JSON written to {OUTPUT_JSON}")

    # ── Render HTML ──
    status_colors = {
        "done": "#10b981",
        "blocked": "#ef4444",
        "in_progress": "#3b82f6",
        "todo": "#6b7280",
        "cancelled": "#9ca3af",
    }

    status_bars = ""
    for status, color in status_colors.items():
        count = by_status.get(status, 0)
        pct = count / total_issues * 100 if total_issues else 0
        status_bars += f"""
        <div style="margin-bottom:8px;">
            <span style="display:inline-block;width:90px;color:#9ca3af;">{status}</span>
            <span style="color:#e2e8f0;font-weight:600;">{count}</span>
            <div style="display:inline-block;width:60%;height:6px;background:#1e293b;border-radius:3px;margin-left:10px;vertical-align:middle;">
                <div style="width:{pct:.0f}%;height:100%;background:{color};border-radius:3px;"></div>
            </div>
            <span style="color:#9ca3af;margin-left:8px;font-size:12px;">{pct:.1f}%</span>
        </div>"""

    stuck_html = ""
    if stuck_issues:
        for s in stuck_issues[:5]:
            stuck_html += f"""
            <tr>
                <td style="color:#f59e0b;">{s['identifier']}</td>
                <td>{s['title']}</td>
                <td style="color:#ef4444;">{s['elapsed_min']} min</td>
            </tr>"""
    else:
        stuck_html = '<tr><td colspan="3" style="color:#10b981;">✅ No stuck issues</td></tr>'

    agent_rows = ""
    for agent, stats in sorted(agent_stats.items(), key=lambda x: -x[1]["total"]):
        agent_success = (stats["done"] / stats["total"] * 100) if stats["total"] > 0 else 0
        agent_rows += f"""
        <tr>
            <td style="color:#93c5fd;">{agent}</td>
            <td>{stats['total']}</td>
            <td style="color:#10b981;">{stats['done']}</td>
            <td style="color:#ef4444;">{stats['blocked']}</td>
            <td style="color:#3b82f6;">{stats['in_progress']}</td>
            <td>{agent_success:.0f}%</td>
        </tr>"""

    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgentNet Metrics — Powered by Paperclip</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0f172a; color:#e2e8f0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; padding:24px; }}
h1 {{ font-size:20px; margin-bottom:4px; }}
.sub {{ color:#64748b; font-size:13px; margin-bottom:24px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:24px; }}
.card {{ background:#1e293b; border-radius:12px; padding:20px; }}
.card .value {{ font-size:32px; font-weight:700; margin-bottom:4px; }}
.card .label {{ color:#64748b; font-size:13px; }}
.card.green .value {{ color:#10b981; }}
.card.red .value {{ color:#ef4444; }}
.card.blue .value {{ color:#3b82f6; }}
.section {{ background:#1e293b; border-radius:12px; padding:20px; margin-bottom:16px; }}
.section h2 {{ font-size:16px; margin-bottom:12px; color:#93c5fd; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th,td {{ text-align:left; padding:8px 12px; border-bottom:1px solid #334155; }}
th {{ color:#64748b; font-weight:500; }}
a {{ color:#60a5fa; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<h1>📊 AgentNet Metrics</h1>
<p class="sub">Powered by Paperclip Control Plane • Generated {now_str} • Auto-refresh every 5 min</p>

<div class="grid">
    <div class="card">
        <div class="value">{total_issues}</div>
        <div class="label">Total Issues</div>
    </div>
    <div class="card green">
        <div class="value">{completed_count}</div>
        <div class="label">Completed</div>
    </div>
    <div class="card red">
        <div class="value">{failed_count}</div>
        <div class="label">Failed / Blocked</div>
    </div>
    <div class="card blue">
        <div class="value">{in_progress_count}</div>
        <div class="label">In Progress</div>
    </div>
    <div class="card">
        <div class="value">{success_rate:.0f}%</div>
        <div class="label">Success Rate</div>
    </div>
    <div class="card">
        <div class="value">{avg_cycle_hours:.1f}h</div>
        <div class="label">Avg Cycle Time</div>
    </div>
</div>

<div class="section">
    <h2>📈 Status Distribution</h2>
    {status_bars}
</div>

<div class="section">
    <h2>⚠️ Stuck Issues (&gt;30 min in_progress)</h2>
    <table>
        <tr><th>ID</th><th>Title</th><th>Elapsed</th></tr>
        {stuck_html}
    </table>
</div>

<div class="section">
    <h2>👤 Per-Agent Stats</h2>
    <table>
        <tr><th>Agent</th><th>Total</th><th>Done</th><th>Blocked</th><th>In Progress</th><th>Success</th></tr>
        {agent_rows}
    </table>
</div>

</body>
</html>"""

    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"[metrics] HTML written to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
