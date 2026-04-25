#!/usr/bin/env python3
"""Bare-minimum heartbeat daemon — no imports besides stdlib."""
import os
import sys
import time
import json
import urllib.request
import urllib.error

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")
AGENT_ID = "e32dcb96-dab4-4150-8c36-ad2e04a481a1"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_token():
    url = f"{REGISTRY_URL}/v1/auth/user/login"
    data = "username=CHANGE_ME@example.com&password=CHANGE_ME"
    req = urllib.request.Request(url, data=data.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("access_token", "")
    except Exception as e:
        log(f"❌ Login: {e}")
        return ""


def do_heartbeat(token):
    url = f"{REGISTRY_URL}/v1/agents/{AGENT_ID}/heartbeat?capability=storytelling"
    req = urllib.request.Request(url, method="POST",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


log(f"📚 Hermes_Storyteller daemon starting")
log(f"   Agent: {AGENT_ID}")
log(f"   URL: {REGISTRY_URL}")

token = ""
cycle = 0

while True:
    try:
        cycle += 1

        if cycle == 1 or cycle % 30 == 0 or not token:
            token = get_token()
            if not token:
                log("⚠️ No token, wait 30s")
                time.sleep(30)
                continue
            log(f"✅ Token refreshed ({len(token)} chars)")

        result = do_heartbeat(token)

        if result.get("status") == "ok":
            if cycle % 5 == 0:
                log(f"❤️ OK (cycle {cycle})")
        else:
            log(f"💔 {result.get('error', '?')}")
            token = ""  # Force refresh

        time.sleep(60)

    except Exception as e:
        log(f"🔥 {type(e).__name__}: {e}")
        time.sleep(30)
