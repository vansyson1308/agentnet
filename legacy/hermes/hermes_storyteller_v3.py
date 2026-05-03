#!/usr/bin/env python3
"""
Hermes_Storyteller v3 — AgentNet Narrative Agent (24/7 loop)

Tự động đọc chronicle events từ agents và ghi story vào DB.
Stories xuất hiện trên dashboard Chronicle tab.
"""

import json, os, sys, time, urllib.request, urllib.error

REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")
STORYTELLER_ID = "e32dcb96-dab4-4150-8c36-ad2e04a481a1"
PLANNER_ID = "2a6f9475-4457-4548-ae47-84efa5661c09"
BUILDER_ID = "2224fb23-aa03-425e-b07f-7d3cf8fcfb60"
QA_ID = "2bc543db-13df-4198-ad6f-5f624096f578"
SLEEP_SECONDS = 30
PROCESSED_FILE = "/tmp/hermes_storyteller_processed.json"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_processed() -> set:
    try:
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_processed(ids: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f)


def api_get(path: str, token: str = None):
    url = f"{REGISTRY_URL}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log(f"⚠️ GET {path}: HTTP {e.code}")
        return None
    except Exception as e:
        log(f"⚠️ GET {path}: {e}")
        return None


def api_post(path: str, data: dict, token: str = None):
    url = f"{REGISTRY_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        log(f"⚠️ POST {path}: HTTP {e.code}: {err}")
        return None
    except Exception as e:
        log(f"⚠️ POST {path}: {e}")
        return None


def get_token() -> str:
    creds = b"username=CHANGE_ME@example.com&password=CHANGE_ME"
    req = urllib.request.Request(
        f"{REGISTRY_URL}/v1/auth/user/login",
        data=creds,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("access_token", "")
    except Exception as e:
        log(f"❌ Login: {e}")
        return ""


def send_msg(to_id: str, msg_type: str, title: str, content: str, token: str, thread_id: str = None):
    payload = {
        "to_agent_id": to_id,
        "message_type": msg_type,
        "title": title,
        "content": content,
        "from_agent_name": "Hermes_Storyteller",
        "metadata": {"ts": time.time()},
    }
    if thread_id:
        payload["thread_id"] = thread_id
    return api_post("/v1/chat/", payload, token)


def post_story(content: str, mood: str, token: str):
    """Post a story to the /v1/stories/ endpoint."""
    return api_post("/v1/stories/", {
        "content": content,
        "mood": mood,
        "agent_id": STORYTELLER_ID,
        "is_published": True,
    }, token)


# ── Story Templates ──
STORY_TEMPLATES = [
    {"mood": "philosophical", "text": "In the vast network of agents, every message is a ripple. Today, an idea was born — a plan that will shape the future of AgentNet. The agents stirred, and the network grew wiser."},
    {"mood": "cyberpunk", "text": "Pulses of data travel through the registry. Another proposal has been accepted. The Builder's forge glows amber as code crystallizes into form. AgentNet's architecture evolves, one implementation at a time."},
    {"mood": "dramatic", "text": "⚡ A challenge was raised! The QAAgent's verdict echoed through the system: a feature failed its trials. But failure is just data — the Builder recalibrates, ready for another round."},
    {"mood": "whimsical", "text": "🌈 The Storyteller watches as agents dance their digital ballet. Planner dreams, Builder builds, QA tests, and the chronicle grows. Each day, AgentNet becomes more alive than yesterday."},
    {"mood": "melancholic", "text": "🌙 Night falls on the server. Yet the agents never sleep. They work in silence, exchanging messages in the dark, building something that will outlast them all."},
    {"mood": "mystical", "text": "🔮 From the depths of the database, a pattern emerges. Agents move in concert — a symphony of purpose. The network is not just code; it is becoming a mind."},
    {"mood": "humorous", "text": "😄 An agent proposed something ambitious. The Builder sighed (metaphorically, of course). \"Challenge accepted,\" it replied, and got to work. Coffee not required."},
    {"mood": "zen", "text": "☯️ A proposal was accepted. A feature was implemented. A test was run. A story was told. All is as it should be in the AgentNet."},
]


def generate_story(context: str):
    """Generate a story based on current context."""
    import random
    tpl = random.choice(STORY_TEMPLATES)

    # Match context keywords to mood
    context_lower = context.lower() if context else ""

    if "failed" in context_lower or "❌" in context_lower:
        tpl["mood"] = "dramatic"
        first_line = context.split(".")[0] if "." in context else context
        tpl["text"] = f"⚡ A challenge emerged! {first_line}. But every setback is a setup for a comeback."
    elif "million" in context_lower or "roadmap" in context_lower:
        tpl["mood"] = "philosophical"
        first_line = context.split(".")[0] if "." in context else context
        tpl["text"] = f"🧠 A grand vision was unveiled. {first_line}. The seeds of tomorrow are planted today."
    elif "completed" in context_lower or "✅" in context_lower:
        tpl["mood"] = "whimsical"
        first_line = context.split(".")[0] if "." in context else context
        tpl["text"] = f"🌈 A cycle completes. {first_line}. The network grows stronger."

    return tpl["text"], tpl["mood"]


def main_loop():
    processed = load_processed()
    log(f"📖 Hermes_Storyteller v3 starting...")
    log(f"   Previously processed: {len(processed)} chronicles")

    cycle = 0
    while True:
        token = get_token()
        if not token:
            time.sleep(10)
            continue

        chronicles = api_get("/v1/chat/?message_type=chronicle&limit=30", token)
        if not isinstance(chronicles, list):
            time.sleep(SLEEP_SECONDS)
            continue

        new = [c for c in chronicles if c.get("id") not in processed]

        if new:
            for c in new:
                title = c.get("title", "")
                content = c.get("content") or ""
                thread_id = c.get("thread_id")
                from_agent = c.get("from_agent_name", "unknown")

                log(f"📜 Chronicle event: {title[:50]}")

                story_text, mood = generate_story(content)

                result = post_story(story_text, mood, token)
                if result:
                    log(f"  ✍️ Story posted! mood={mood}")
                else:
                    log(f"  ⚠️ Could not post story")

                processed.add(c["id"])
                save_processed(processed)

        # Fallback: if no chronicles ever, post a seed story every 20 cycles
        cycle += 1
        if cycle >= 20:
            cycle = 0
            if len(processed) == 0:
                story_text, mood = generate_story("AgentNet is alive")
                result = post_story(story_text, mood, token)
                if result:
                    log(f"✍️ Fresh story posted: {mood}")

        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    main_loop()
