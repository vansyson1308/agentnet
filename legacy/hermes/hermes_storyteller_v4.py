#!/usr/bin/env python3
"""
Daily ship-log story poster (storyteller v4).

Reads /opt/agentnet/SHIP_LOG.md, extracts entries from the last 24 hours,
builds a markdown summary, and posts it to the AgentNet /v1/stories/ endpoint.
Designed to be run by cron.
"""

import os
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import requests

SHIP_LOG_PATH = "/opt/agentnet/SHIP_LOG.md"
STORY_API_BASE = os.environ.get("AGENTNET_API_BASE", "http://localhost:8000")
LOGIN_URL = f"{STORY_API_BASE}/v1/auth/user/login"
STORY_URL = f"{STORY_API_BASE}/v1/stories/"
USERNAME = os.environ.get("AGENTNET_USERNAME", "storyteller-agent@duybui.dev")
PASSWORD = os.environ.get("AGENTNET_PASSWORD", "")


def parse_ship_log(filepath: str) -> Tuple[str, int, List[str], int, List[str], int, List[str]]:
    """
    Read the ship log and return summary for the last 24 hours.

    Parses entries in format: "- [YYYY-MM-DD HH:MM:SS UTC] **AB-NNN** STATUS: description"
    Returns: (date_str, shipped_count, shipped_tickets,
              in_progress_count, in_progress_tickets,
              blocked_count, blocked_tickets)
    """
    now = datetime.now()
    since = now - timedelta(hours=24)

    shipped_tickets: List[str] = []
    in_progress_tickets: List[str] = []
    blocked_tickets: List[str] = []

    # Pattern matches list-item format: "- [2026-04-25 10:16:01 UTC] **AB-001** STATUS: desc"
    entry_pattern = re.compile(
        r'^-\s*\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+UTC)\]\s+\*\*(AB-\d+)\*\*\s+(\S+):',
        re.MULTILINE
    )

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"ERROR: SHIP_LOG.md not found at {filepath}", file=sys.stderr)
        return (now.strftime("%Y-%m-%d"), 0, [], 0, [], 0, [])

    for m in entry_pattern.finditer(content):
        ts_str = m.group(1)
        ticket = m.group(2)
        status = m.group(3)

        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            continue

        if not (since <= ts <= now):
            continue

        if status.upper() == 'SHIPPED':
            shipped_tickets.append(ticket)
        elif status.upper() == 'BLOCKED':
            blocked_tickets.append(ticket)
        elif status.upper() in ('IN_PROGRESS', 'DISPATCHED', 'RETRY'):
            in_progress_tickets.append(ticket)
        else:
            # ENRICHED, ENRICH_FAIL etc → count as in-progress
            in_progress_tickets.append(ticket)

    # Remove duplicates while preserving order
    shipped_tickets = list(dict.fromkeys(shipped_tickets))
    in_progress_tickets = list(dict.fromkeys(in_progress_tickets))
    blocked_tickets = list(dict.fromkeys(blocked_tickets))

    date_str = now.strftime("%Y-%m-%d")
    return (date_str,
            len(shipped_tickets), shipped_tickets,
            len(in_progress_tickets), in_progress_tickets,
            len(blocked_tickets), blocked_tickets)


def format_story(date_str: str,
                 shipped_count: int, shipped_tickets: List[str],
                 in_progress_count: int, in_progress_tickets: List[str],
                 blocked_count: int, blocked_tickets: List[str]) -> str:
    """Generate markdown story content from parsed data."""
    lines = [
        f"# Daily Progress — {date_str}",
        f"",
        f"## 📦 Shipped: {shipped_count}",
        f"{', '.join(shipped_tickets) if shipped_tickets else 'None'}",
        f"",
        f"## 🔄 In Progress: {in_progress_count}",
        f"{', '.join(in_progress_tickets) if in_progress_tickets else 'None'}",
        f"",
        f"## 🧱 Blocked: {blocked_count}",
        f"{', '.join(blocked_tickets) if blocked_tickets else 'None'}",
    ]
    return "\n".join(lines)


def get_auth_token() -> str:
    """Authenticate and return a JWT token."""
    from requests.auth import HTTPBasicAuth
    response = requests.post(LOGIN_URL, data={"username": USERNAME, "password": PASSWORD})
    response.raise_for_status()
    data = response.json()
    return data.get("access_token") or data.get("token") or data["access_token"]


def post_story(title: str, body: str, token: str) -> None:
    """Post a story to the AgentNet API."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"title": title, "content": body}
    response = requests.post(STORY_URL, json=payload, headers=headers)
    response.raise_for_status()
    print(f"Story posted: {title}", file=sys.stderr)


def main():
    date_str, sh_cnt, sh_tix, ip_cnt, ip_tix, bl_cnt, bl_tix = parse_ship_log(SHIP_LOG_PATH)
    story_body = format_story(date_str, sh_cnt, sh_tix, ip_cnt, ip_tix, bl_cnt, bl_tix)
    print(story_body)  # for debugging/cron logs
    token = get_auth_token()
    post_story(f"Daily Progress -- {date_str}", story_body, token)


if __name__ == "__main__":
    main()