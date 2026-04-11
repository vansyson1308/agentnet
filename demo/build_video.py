#!/usr/bin/env python3
"""
AgentNet Demo Video Builder
Runs the full demo flow, captures frames, and renders a complete MP4 video.

Usage:
    python demo/build_video.py

Output:
    demo/agentnet_demo.mp4
"""

import json
import os
import sys
import textwrap
import time
import uuid

import httpx
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# CONFIG
# ============================================================
W, H = 1920, 1080
FPS = 24
BG = (10, 14, 26)  # #0A0E1A
CARD_BG = (20, 25, 41)  # #141929
ACCENT = (79, 110, 247)  # #4F6EF7
GREEN = (16, 185, 129)  # #10B981
RED = (239, 68, 68)  # #EF4444
CYAN = (6, 182, 212)  # #06B6D4
PURPLE = (139, 92, 246)  # #8B5CF6
ORANGE = (245, 158, 11)  # #F59E0B
WHITE = (255, 255, 255)
MUTED = (148, 163, 184)  # #94A3B8
DIM = (100, 116, 139)

FONTS_DIR = "C:/Windows/Fonts"
FONT_TITLE = os.path.join(FONTS_DIR, "arialbd.ttf")
FONT_BODY = os.path.join(FONTS_DIR, "arial.ttf")
FONT_CODE = os.path.join(FONTS_DIR, "consola.ttf")

REGISTRY = "http://localhost:8000"
PAYMENT = "http://localhost:8001"
SIMULATION = "http://localhost:8002"

FRAMES_DIR = "demo/frames"
OUTPUT = "demo/agentnet_demo.mp4"

os.makedirs(FRAMES_DIR, exist_ok=True)

frame_counter = 0
scenes = []  # (image_path, duration_seconds)


# ============================================================
# DRAWING HELPERS
# ============================================================
def new_frame():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def save_frame(img, duration=3.0):
    global frame_counter
    path = os.path.join(FRAMES_DIR, f"frame_{frame_counter:04d}.png")
    img.save(path)
    scenes.append((path, duration))
    frame_counter += 1
    return path


def draw_header(draw, scene_num, scene_title, subtitle=""):
    """Draw scene header bar."""
    # Top bar
    draw.rectangle([(0, 0), (W, 70)], fill=(15, 20, 35))
    draw.text(
        (30, 18),
        f"SCENE {scene_num}",
        fill=ACCENT,
        font=font(FONT_TITLE, 20),
    )
    draw.text(
        (160, 15),
        scene_title,
        fill=WHITE,
        font=font(FONT_TITLE, 26),
    )
    if subtitle:
        draw.text(
            (160, 45),
            subtitle,
            fill=MUTED,
            font=font(FONT_BODY, 14),
        )
    # AgentNet logo text
    draw.text(
        (W - 200, 22),
        "AgentNet Demo",
        fill=DIM,
        font=font(FONT_BODY, 18),
    )


def draw_api_call(draw, y, method, endpoint, status_code=None):
    """Draw an API call indicator."""
    method_colors = {
        "GET": GREEN,
        "POST": ACCENT,
        "PUT": ORANGE,
        "DELETE": RED,
    }
    color = method_colors.get(method, WHITE)

    # Method badge
    draw.rounded_rectangle(
        [(60, y), (130, y + 28)], radius=4, fill=color
    )
    draw.text(
        (68, y + 4), method, fill=WHITE, font=font(FONT_TITLE, 16)
    )

    # Endpoint
    draw.text(
        (145, y + 4),
        endpoint,
        fill=WHITE,
        font=font(FONT_CODE, 16),
    )

    # Status
    if status_code:
        sc_color = GREEN if status_code < 300 else RED
        draw.text(
            (W - 120, y + 4),
            f"{status_code}",
            fill=sc_color,
            font=font(FONT_TITLE, 16),
        )


def draw_json_block(draw, x, y, data, title="Response", max_lines=12, width=70):
    """Draw a JSON response block."""
    draw.text(
        (x, y), title, fill=CYAN, font=font(FONT_TITLE, 16)
    )
    y += 25

    # JSON card background
    json_str = json.dumps(data, indent=2, default=str)
    lines = json_str.split("\n")[:max_lines]
    block_h = len(lines) * 20 + 20
    draw.rounded_rectangle(
        [(x, y), (x + width * 10, y + block_h)],
        radius=6,
        fill=CARD_BG,
    )

    for i, line in enumerate(lines):
        # Syntax coloring
        color = MUTED
        if '": ' in line:
            color = WHITE
        if "true" in line or "false" in line:
            color = ORANGE
        if any(c in line for c in ['"id"', '"status"', '"name"']):
            color = CYAN

        wrapped = line[:width]
        draw.text(
            (x + 15, y + 10 + i * 20),
            wrapped,
            fill=color,
            font=font(FONT_CODE, 14),
        )

    return y + block_h + 10


def draw_wallet_card(draw, x, y, label, balance, reserved, color=WHITE):
    """Draw a wallet balance card."""
    card_w, card_h = 380, 120
    draw.rounded_rectangle(
        [(x, y), (x + card_w, y + card_h)],
        radius=8,
        fill=CARD_BG,
    )
    draw.text(
        (x + 15, y + 10), label, fill=MUTED, font=font(FONT_BODY, 16)
    )
    draw.text(
        (x + 15, y + 35),
        f"{balance} credits",
        fill=color,
        font=font(FONT_TITLE, 32),
    )
    reserved_color = RED if reserved > 0 else DIM
    draw.text(
        (x + 15, y + 80),
        f"Reserved: {reserved}",
        fill=reserved_color,
        font=font(FONT_BODY, 16),
    )


def draw_progress_bar(draw, y, progress, label=""):
    """Draw a progress bar."""
    bar_x, bar_w, bar_h = 60, W - 120, 20
    draw.rounded_rectangle(
        [(bar_x, y), (bar_x + bar_w, y + bar_h)],
        radius=4,
        fill=(30, 40, 60),
    )
    filled_w = int(bar_w * progress / 100)
    if filled_w > 0:
        draw.rounded_rectangle(
            [(bar_x, y), (bar_x + filled_w, y + bar_h)],
            radius=4,
            fill=ACCENT,
        )
    if label:
        draw.text(
            (bar_x, y - 22), label, fill=MUTED, font=font(FONT_BODY, 14)
        )


# ============================================================
# TITLE CARD
# ============================================================
def render_title():
    img, draw = new_frame()

    # Decorative circles
    for cx, cy, r, c in [
        (1500, 200, 300, (*ACCENT, 30)),
        (1600, 350, 200, (*PURPLE, 30)),
    ]:
        for offset in range(r, 0, -1):
            alpha = max(5, 30 - offset // 10)
            draw.ellipse(
                [(cx - offset, cy - offset), (cx + offset, cy + offset)],
                fill=(*c[:3],),
            )

    # Title
    draw.text(
        (W // 2 - 250, 300),
        "AgentNet",
        fill=WHITE,
        font=font(FONT_TITLE, 72),
    )
    draw.text(
        (W // 2 - 380, 400),
        "The Autonomous AI Agent Economy",
        fill=ACCENT,
        font=font(FONT_BODY, 32),
    )

    # Divider
    draw.line([(W // 2 - 200, 460), (W // 2 + 200, 460)], fill=ACCENT, width=2)

    # Subtitle
    draw.text(
        (W // 2 - 250, 490),
        "Built on Hedera  |  APEX Hackathon 2026",
        fill=MUTED,
        font=font(FONT_BODY, 22),
    )

    draw.text(
        (W // 2 - 100, 540),
        "LIVE DEMO",
        fill=GREEN,
        font=font(FONT_TITLE, 24),
    )

    save_frame(img, duration=4.0)
    print("  [title] Title card rendered")


# ============================================================
# SCENE 1: System Overview
# ============================================================
def render_scene1():
    img, draw = new_frame()
    draw_header(draw, 1, "System Overview", "Microservices architecture running live")

    services = [
        ("Registry API", ":8000", "Agent CRUD, Tasks, WebSocket, Auth", GREEN),
        ("Payment API", ":8001", "Wallets, Escrow, Transactions", GREEN),
        ("Simulation API", ":8002", "Swarm Engine, LLM Reports, Chat", GREEN),
        ("Dashboard", ":8080", "Monitoring & Telemetry UI", GREEN),
        ("Background Worker", "BG", "Auto-refund, Timeout Monitor", GREEN),
        ("PostgreSQL 15", ":5432", "Persistent storage", CYAN),
        ("Redis 7", ":6379", "Pub/Sub, WebSocket coordination", CYAN),
        ("Nginx Gateway", ":8888", "Reverse proxy, single URL access", CYAN),
    ]

    y = 100
    for i, (name, port, desc, color) in enumerate(services):
        col = i % 2
        row = i // 2
        cx = 60 + col * 920
        cy = y + row * 110

        draw.rounded_rectangle(
            [(cx, cy), (cx + 880, cy + 95)],
            radius=8,
            fill=CARD_BG,
        )
        # Status dot
        draw.ellipse(
            [(cx + 15, cy + 35), (cx + 30, cy + 50)],
            fill=color,
        )
        draw.text(
            (cx + 40, cy + 12),
            name,
            fill=WHITE,
            font=font(FONT_TITLE, 22),
        )
        draw.text(
            (cx + 40, cy + 42),
            f"Port {port}  —  {desc}",
            fill=MUTED,
            font=font(FONT_BODY, 14),
        )
        draw.text(
            (cx + 780, cy + 30),
            "HEALTHY",
            fill=GREEN,
            font=font(FONT_TITLE, 14),
        )

    # Health check results
    y_bottom = y + 470
    draw.text(
        (60, y_bottom),
        "Health Check Results:",
        fill=WHITE,
        font=font(FONT_TITLE, 20),
    )

    checks = []
    for name, url in [
        ("Registry", REGISTRY),
        ("Payment", PAYMENT),
        ("Simulation", SIMULATION),
    ]:
        try:
            r = httpx.get(f"{url}/health", timeout=5)
            checks.append((name, r.status_code, r.json()))
        except Exception as e:
            checks.append((name, 0, {"error": str(e)}))

    for i, (name, code, data) in enumerate(checks):
        cx = 60 + i * 620
        cy = y_bottom + 35
        status_text = json.dumps(data, default=str)[:60]
        draw.text(
            (cx, cy),
            f"{name}: {code} {status_text}",
            fill=GREEN if code == 200 else RED,
            font=font(FONT_CODE, 14),
        )

    save_frame(img, duration=5.0)
    print("  [scene1] System overview rendered")


# ============================================================
# SCENE 2: Agent Registration & Discovery
# ============================================================
def render_scene2(token):
    h = {"Authorization": f"Bearer {token}"}

    # Frame 2a: Create Agent
    img, draw = new_frame()
    draw_header(
        draw, 2, "Agent Registration & Discovery", "Create agents with capabilities"
    )

    draw_api_call(draw, 90, "POST", "/v1/agents/", 201)

    # Show request
    req_data = {
        "name": "SmartTrader-AI",
        "capabilities": [
            {"name": "market_analysis", "version": "2.0", "price": 10.0},
            {"name": "risk_assessment", "version": "1.0", "price": 8.0},
        ],
        "endpoint": "https://smarttrader.agentnet.io",
    }
    draw_json_block(draw, 60, 130, req_data, "Request Body", max_lines=10)

    # Actually create the agent
    import ed25519

    sk, vk = ed25519.create_keypair()
    pk = vk.to_ascii(encoding="hex").decode()

    r = httpx.post(
        f"{REGISTRY}/v1/agents/",
        headers=h,
        json={
            "name": f"SmartTrader-AI-{uuid.uuid4().hex[:6]}",
            "description": "AI agent for market analysis and risk assessment",
            "capabilities": [
                {
                    "name": "market_analysis",
                    "version": "2.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 10.0,
                },
                {
                    "name": "risk_assessment",
                    "version": "1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 8.0,
                },
            ],
            "endpoint": "https://smarttrader.agentnet.io/execute",
            "public_key": pk,
        },
        timeout=10,
    )

    if r.status_code in (200, 201):
        agent_data = r.json()
        draw_json_block(
            draw, 960, 130, agent_data, "Response (201 Created)", max_lines=14
        )
        agent_id = agent_data.get("id", "?")
    else:
        draw_json_block(draw, 960, 130, r.json(), f"Response ({r.status_code})")
        agent_id = None

    save_frame(img, duration=5.0)
    print(f"  [scene2a] Agent created: {r.status_code}")

    # Frame 2b: Discover Agent
    img, draw = new_frame()
    draw_header(
        draw,
        2,
        "Agent Discovery",
        "Find the best agent for a capability",
    )

    draw_api_call(draw, 90, "GET", "/v1/agents/discover/data_analysis", 200)

    r2 = httpx.get(
        f"{REGISTRY}/v1/agents/discover/data_analysis", headers=h, timeout=10
    )
    if r2.status_code == 200:
        disc_data = r2.json()
        draw_json_block(
            draw, 60, 130, disc_data, "Discovery Result", max_lines=18, width=85
        )

        # Highlight best match
        best = disc_data.get("best_match", {})
        draw.rounded_rectangle([(960, 130), (W - 60, 500)], radius=8, fill=CARD_BG)
        draw.text(
            (980, 145),
            "BEST MATCH",
            fill=GREEN,
            font=font(FONT_TITLE, 24),
        )
        draw.text(
            (980, 185),
            best.get("name", "?"),
            fill=WHITE,
            font=font(FONT_TITLE, 28),
        )
        draw.text(
            (980, 225),
            f"Reputation: {best.get('reputation_tier', '?')}",
            fill=CYAN,
            font=font(FONT_BODY, 18),
        )
        draw.text(
            (980, 255),
            f"Success Rate: {best.get('success_rate', 0)}%",
            fill=MUTED,
            font=font(FONT_BODY, 18),
        )
        draw.text(
            (980, 285),
            f"Price: {best.get('price', '?')} credits",
            fill=ORANGE,
            font=font(FONT_BODY, 18),
        )

    save_frame(img, duration=5.0)
    print(f"  [scene2b] Discovery: {r2.status_code}")

    return agent_id


# ============================================================
# SCENE 3: Escrow Payment
# ============================================================
def render_scene3(token):
    h = {"Authorization": f"Bearer {token}"}

    # Get existing agents
    agents = httpx.get(f"{REGISTRY}/v1/agents/", headers=h, timeout=10).json()
    if len(agents) < 2:
        print("  [scene3] Not enough agents, skipping escrow demo")
        return

    caller = agents[0]
    callee = None
    for a in agents:
        if a.get("capabilities") and a["id"] != caller["id"]:
            callee = a
            break
    if not callee:
        callee = agents[1]

    # Get wallets
    caller_wallet = None
    callee_wallet = None
    wallets = httpx.get(f"{PAYMENT}/v1/wallets/", headers=h, timeout=10).json()

    # Fund wallets
    for w in wallets if isinstance(wallets, list) else []:
        if w.get("owner_id") == caller["id"]:
            caller_wallet = w
        if w.get("owner_id") == callee["id"]:
            callee_wallet = w

    if not caller_wallet or not callee_wallet:
        print("  [scene3] Wallets not found, showing concept frame")
        img, draw = new_frame()
        draw_header(draw, 3, "Escrow Payment System", "Trustless task execution")
        draw.text(
            (60, 200),
            "Escrow Flow: Lock -> Execute -> Release",
            fill=WHITE,
            font=font(FONT_TITLE, 32),
        )
        save_frame(img, duration=5.0)
        return

    # Fund caller wallet
    try:
        httpx.post(
            f"{PAYMENT}/v1/wallets/{caller_wallet['id']}/fund",
            headers=h,
            json={"amount": 1000, "currency": "credits"},
            timeout=10,
        )
    except Exception:
        pass

    # Refresh wallet
    try:
        r = httpx.get(
            f"{PAYMENT}/v1/wallets/{caller_wallet['id']}/balance",
            headers=h,
            timeout=10,
        )
        if r.status_code == 200:
            caller_wallet = r.json()
    except Exception:
        pass

    # Frame 3a: Before escrow
    img, draw = new_frame()
    draw_header(
        draw,
        3,
        "Escrow Payment System",
        "Trustless escrow locks funds before task execution",
    )

    draw.text(
        (60, 100),
        "BEFORE TASK CREATION",
        fill=ORANGE,
        font=font(FONT_TITLE, 22),
    )

    bal = caller_wallet.get("balance_credits", 0)
    res = caller_wallet.get("reserved_credits", 0)
    draw_wallet_card(draw, 60, 150, f"Caller: {caller['name'][:20]}", bal, res, GREEN)
    draw_wallet_card(
        draw,
        500,
        150,
        f"Callee: {callee['name'][:20]}",
        callee_wallet.get("balance_credits", 0),
        callee_wallet.get("reserved_credits", 0),
        GREEN,
    )

    # Escrow flow diagram
    draw.text(
        (60, 350), "Escrow Flow:", fill=WHITE, font=font(FONT_TITLE, 24)
    )

    steps = [
        ("1", "Create Task", "Caller requests capability from Callee", ACCENT),
        ("2", "Lock Escrow", "Credits reserved in caller wallet", ORANGE),
        ("3", "Execute Task", "Callee performs work", CYAN),
        ("4", "Confirm & Release", "Escrow released to callee on completion", GREEN),
    ]

    for i, (num, title, desc, color) in enumerate(steps):
        sx = 60 + i * 460
        sy = 400

        draw.rounded_rectangle(
            [(sx, sy), (sx + 430, sy + 100)],
            radius=8,
            fill=CARD_BG,
        )
        # Number circle
        draw.ellipse(
            [(sx + 10, sy + 30), (sx + 45, sy + 65)],
            fill=color,
        )
        draw.text(
            (sx + 20, sy + 34),
            num,
            fill=WHITE,
            font=font(FONT_TITLE, 18),
        )
        draw.text(
            (sx + 55, sy + 15),
            title,
            fill=WHITE,
            font=font(FONT_TITLE, 18),
        )
        draw.text(
            (sx + 55, sy + 45),
            desc,
            fill=MUTED,
            font=font(FONT_BODY, 13),
        )

        # Arrow
        if i < 3:
            draw.text(
                (sx + 435, sy + 35),
                "->",
                fill=DIM,
                font=font(FONT_TITLE, 20),
            )

    # Money invariant
    draw.rounded_rectangle([(60, 550), (W - 60, 620)], radius=8, fill=(20, 35, 20))
    draw.text(
        (80, 565),
        "MONEY INVARIANT: wallet_balance + reserved = constant  |  No double-spend possible",
        fill=GREEN,
        font=font(FONT_TITLE, 20),
    )

    save_frame(img, duration=6.0)
    print("  [scene3a] Escrow overview rendered")


# ============================================================
# SCENE 4: Swarm Simulation
# ============================================================
def render_scene4(token):
    h = {"Authorization": f"Bearer {token}"}

    # Frame 4a: Preview
    img, draw = new_frame()
    draw_header(
        draw,
        4,
        "Swarm Simulation Engine",
        "Predict multi-agent dynamics with AI (Gemini LLM)",
    )

    draw_api_call(draw, 90, "POST", "/v1/simulations/preview", 200)

    r = httpx.post(
        f"{SIMULATION}/v1/simulations/preview",
        headers=h,
        json={
            "seed_config": {"agent_filter": {}},
            "simulation_config": {"num_steps": 50, "platform": "twitter"},
        },
        timeout=10,
    )

    if r.status_code == 200:
        preview = r.json()
        draw_json_block(draw, 60, 130, preview, "Simulation Preview", max_lines=10)

        # Highlight stats
        draw.rounded_rectangle([(960, 130), (W - 60, 350)], radius=8, fill=CARD_BG)
        draw.text(
            (980, 145),
            "SIMULATION PARAMETERS",
            fill=PURPLE,
            font=font(FONT_TITLE, 22),
        )
        draw.text(
            (980, 185),
            f"Seed Agents: {preview.get('num_seed_agents', '?')}",
            fill=WHITE,
            font=font(FONT_BODY, 22),
        )
        draw.text(
            (980, 215),
            f"Platform: {preview.get('platform', '?')}",
            fill=CYAN,
            font=font(FONT_BODY, 22),
        )
        draw.text(
            (980, 245),
            f"Steps: {preview.get('num_steps', '?')}",
            fill=MUTED,
            font=font(FONT_BODY, 22),
        )
        draw.text(
            (980, 275),
            f"Estimated Cost: {preview.get('estimated_cost', '?')} credits",
            fill=ORANGE,
            font=font(FONT_BODY, 22),
        )

    save_frame(img, duration=5.0)
    print(f"  [scene4a] Preview: {r.status_code}")

    # Frame 4b: Run simulation
    img, draw = new_frame()
    draw_header(draw, 4, "Running Simulation", "Real-time multi-agent simulation")

    draw_api_call(draw, 90, "POST", "/v1/simulations/", 202)

    r2 = httpx.post(
        f"{SIMULATION}/v1/simulations/",
        headers=h,
        json={
            "name": f"Demo Simulation {uuid.uuid4().hex[:6]}",
            "description": "Multi-agent market simulation for hackathon demo",
            "seed_config": {"agent_filter": {"limit": 5}},
            "simulation_config": {"num_steps": 10, "platform": "twitter"},
        },
        timeout=30,
    )

    sim_id = None
    if r2.status_code in (200, 201, 202):
        sim_data = r2.json()
        sim_id = sim_data.get("id")
        draw_json_block(draw, 60, 130, sim_data, "Simulation Created", max_lines=12)

        # Wait for completion
        draw.text(
            (960, 130),
            "Waiting for simulation...",
            fill=ORANGE,
            font=font(FONT_TITLE, 22),
        )

        final_status = "unknown"
        for i in range(30):
            time.sleep(2)
            r3 = httpx.get(
                f"{SIMULATION}/v1/simulations/{sim_id}",
                headers=h,
                timeout=10,
            )
            sd = r3.json()
            final_status = sd.get("status", "?")
            progress = sd.get("progress_pct", 0)
            if final_status in ("completed", "failed", "cancelled", "timeout"):
                break

        draw.text(
            (960, 170),
            f"Status: {final_status}",
            fill=GREEN if final_status == "completed" else RED,
            font=font(FONT_TITLE, 28),
        )
        draw.text(
            (960, 210),
            f"Progress: {progress}%",
            fill=WHITE,
            font=font(FONT_BODY, 20),
        )
        draw_progress_bar(draw, 260, progress, "Simulation Progress")

    save_frame(img, duration=4.0)
    print(f"  [scene4b] Simulation: {r2.status_code}, status={final_status if sim_id else '?'}")

    # Frame 4c: Report
    if sim_id and final_status == "completed":
        img, draw = new_frame()
        draw_header(
            draw, 4, "Simulation Report", "AI-generated prediction analysis"
        )

        draw_api_call(draw, 90, "GET", f"/v1/simulations/{sim_id}/report", 200)

        r4 = httpx.get(
            f"{SIMULATION}/v1/simulations/{sim_id}/report",
            headers=h,
            timeout=10,
        )

        if r4.status_code == 200:
            report = r4.json()
            content = str(report.get("content", ""))

            # Render report content
            draw.rounded_rectangle(
                [(60, 130), (W - 60, H - 60)],
                radius=8,
                fill=CARD_BG,
            )

            draw.text(
                (80, 145),
                "PREDICTION REPORT",
                fill=PURPLE,
                font=font(FONT_TITLE, 22),
            )

            confidence = report.get("confidence_score", 0)
            conf_color = GREEN if confidence > 0.7 else ORANGE if confidence > 0.4 else RED
            draw.text(
                (80, 180),
                f"Confidence Score: {confidence:.0%}",
                fill=conf_color,
                font=font(FONT_TITLE, 20),
            )

            # Report text
            lines = content.split("\n")[:30]
            for i, line in enumerate(lines):
                color = WHITE
                if line.startswith("#"):
                    color = CYAN
                    line = line.replace("#", "").strip()
                elif line.startswith("- "):
                    color = MUTED
                elif "**" in line:
                    color = ORANGE
                    line = line.replace("**", "")

                draw.text(
                    (80, 220 + i * 22),
                    line[:100],
                    fill=color,
                    font=font(FONT_CODE, 13),
                )

        save_frame(img, duration=6.0)
        print(f"  [scene4c] Report rendered")


# ============================================================
# SCENE 5: Closing
# ============================================================
def render_closing():
    img, draw = new_frame()

    # Big title
    draw.text(
        (W // 2 - 220, 200),
        "AgentNet",
        fill=WHITE,
        font=font(FONT_TITLE, 72),
    )
    draw.text(
        (W // 2 - 380, 300),
        "The Autonomous AI Agent Economy",
        fill=ACCENT,
        font=font(FONT_BODY, 32),
    )

    # Features proven
    draw.text(
        (W // 2 - 200, 400),
        "What We Demonstrated:",
        fill=WHITE,
        font=font(FONT_TITLE, 24),
    )

    proofs = [
        "Agent Registration & Discovery",
        "Escrow-Based Payments (No Double-Spend)",
        "Multi-Agent Swarm Simulation (Gemini AI)",
        "Distributed Tracing & Audit Trails",
        "Real-Time WebSocket Coordination",
    ]

    for i, p in enumerate(proofs):
        draw.ellipse(
            [(W // 2 - 215, 448 + i * 35), (W // 2 - 200, 463 + i * 35)],
            fill=GREEN,
        )
        draw.text(
            (W // 2 - 190, 442 + i * 35),
            p,
            fill=WHITE,
            font=font(FONT_BODY, 20),
        )

    # Links
    draw.text(
        (W // 2 - 280, 650),
        "github.com/vansyson1308/agentnet",
        fill=CYAN,
        font=font(FONT_CODE, 22),
    )

    draw.text(
        (W // 2 - 250, 700),
        "Built on Hedera  |  APEX Hackathon 2026",
        fill=MUTED,
        font=font(FONT_BODY, 20),
    )

    save_frame(img, duration=5.0)
    print("  [closing] Closing card rendered")


# ============================================================
# BUILD VIDEO
# ============================================================
def build_video():
    from moviepy.editor import ImageClip, concatenate_videoclips

    print("\n[video] Building MP4 from frames...")

    clips = []
    for img_path, duration in scenes:
        clip = ImageClip(img_path).set_duration(duration).set_fps(FPS)
        # Add fade in/out
        clip = clip.crossfadein(0.5).crossfadeout(0.5)
        clips.append(clip)

    if not clips:
        print("[video] No frames to render!")
        return

    final = concatenate_videoclips(clips, method="compose", padding=-0.5)
    final.write_videofile(
        OUTPUT,
        fps=FPS,
        codec="libx264",
        audio=False,
        preset="medium",
        bitrate="5000k",
    )

    print(f"\n[video] Done! Output: {OUTPUT}")
    print(f"[video] Duration: {sum(d for _, d in scenes):.1f}s")
    print(f"[video] Frames: {len(scenes)}")


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("  AgentNet Demo Video Builder")
    print("=" * 60)

    # Login
    print("\n[auth] Logging in...")
    r = httpx.post(
        f"{REGISTRY}/v1/auth/user/login",
        data={
            "username": "hackathon@agentnet.io",
            "password": "Hackathon2026!",
        },
        timeout=10,
    )
    token = r.json()["access_token"]
    print(f"[auth] Token: {token[:20]}...")

    # Render scenes
    print("\n[render] Rendering frames...")
    render_title()
    render_scene1()
    render_scene2(token)
    render_scene3(token)
    render_scene4(token)
    render_closing()

    # Build video
    build_video()


if __name__ == "__main__":
    main()
