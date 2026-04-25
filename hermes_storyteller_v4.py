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
USERNAME = os.environ.get("AGENTNET_USERNAME", "agent")
PASSWORD = os.environ.get("AGENTNET_PASSWORD", "password")


def parse_ship_log(filepath: str) -> Tuple[str, int, List[str], int, List[str], int, List[str]]:
    """
    Read the ship log and return summary for the last 24 hours.

    Returns a tuple: (date_str, shipped_count, shipped_tickets,
                      in_progress_count, in_progress_tickets,
                      blocked_count, blocked_tickets)
    """
    now = datetime.now()
    since = now - timedelta(hours=24)

    shipped_tickets: List[str] = []
    in_progress_tickets: List[str] = []
    blocked_tickets: List[str] = []

    ticket_pattern = re.compile(r'(AB-\d+)')  # simple ticket ID pattern
    status_patterns = {
        'shipped': re.compile(r'\bshipped\b', re.IGNORECASE),
        'in_progress': re.compile(r'\bin\s*progress\b', re.IGNORECASE),
        'blocked': re.compile(r'\bblocked\b', re.IGNORECASE),
    }

    # Track current date from headers like "# 2025-04-25" or "## 2025-04-25"
    current_section_date = None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"ERROR: SHIP_LOG.md not found at {filepath}", file=sys.stderr)
        return (now.strftime("%Y-%m-%d"), 0, [], 0, [], 0, [])

    for line in lines:
        line_stripped = line.strip()

        # Check if this line is a section header with a date
        date_match = re.match(r'^#+\s*(\d{4}-\d{2}-\d{2})\s*', line_stripped)
        if date_match:
            section_date_str = date_match.group(1)
            try:
                section_date = datetime.strptime(section_date_str, "%Y-%m-%d")
                # If the section date is within the last 24 hours, we process it
                if since <= section_date <= now:
                    current_section_date = section_date
                else:
                    current_section_date = None
            except ValueError:
                current_section_date = None
            continue

        # Only process lines under a valid (within 24h) section header
        if current_section_date is None:
            continue

        # Extract ticket IDs
        ticket_ids = ticket_pattern.findall(line_stripped)
        if not ticket_ids:
            continue

        # Determine status from the line
        line_lower = line_stripped.lower()
        if status_patterns['shipped'].search(line_lower):
            shipped_tickets.extend(ticket_ids)
        elif status_patterns['in_progress'].search(line_lower):
            in_progress_tickets.extend(ticket_ids)
        elif status_patterns['blocked'].search(line_lower):
            blocked_tickets.extend(ticket_ids)
        else:
            # If no explicit status, treat as shipped (common log assumption)
            shipped_tickets.extend(ticket_ids)

    # Remove duplicates while preserving order (optional)
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
        f"# Daily Progress -- {date_str}",
        f"Shipped today ({shipped_count}): {', '.join(shipped_tickets) if shipped_tickets else ''}",
        f"In progress ({in_progress_count}): {', '.join(in_progress_tickets) if in_progress_tickets else ''}",
        f"Blocked ({blocked_count}): {', '.join(blocked_tickets) if blocked_tickets else ''}",
    ]
    return "\n".join(lines)


def get_auth_token() -> str:
    """Authenticate and return a JWT token."""
    payload = {"username": USERNAME, "password": PASSWORD}
    response = requests.post(LOGIN_URL, json=payload)
    response.raise_for_status()
    data = response.json()
    # token key could be "access_token" or "token"
    return data.get("access_token") or data.get("token") or data["access_token"]


def post_story(title: str, body: str, token: str) -> None:
    """Post a story to the AgentNet API."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"title": title, "body": body}
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