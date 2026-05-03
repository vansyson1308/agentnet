#!/usr/bin/env python3
"""
Werewolf v2 — Live Game with REAL DeepSeek LLM agents
========================================================
Each agent gets ONLY their observation (no hidden state leak).
Agents reason freely based on what they see.
"""
import sys
import os
import json
import time
import random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import GameState, GameConfig, Role, Phase
from game_loop import (
    create_game, run_game_next_phase, save_game_state, load_game_state,
    format_observation_for_prompt,
)

AGENT_POOL = [
    {"id": "hermes-planner",    "name": "Planner"},
    {"id": "hermes-builder",    "name": "Builder"},
    {"id": "hermes-qaagent",    "name": "QAAgent"},
    {"id": "hermes-storyteller","name": "Storyteller"},
    {"id": "echo",              "name": "Echo"},
    {"id": "poll",              "name": "Poll"},
    {"id": "openclaw",          "name": "OpenClaw"},
    {"id": "hermes-builder-v6", "name": "BuilderV6"},
    {"id": "shadow",            "name": "Shadow"},
    {"id": "ember",             "name": "Ember"},
    {"id": "frost",             "name": "Frost"},
    {"id": "blitz",             "name": "Blitz"},
    {"id": "nova",              "name": "Nova"},
    {"id": "vex",               "name": "Vex"},
    {"id": "drift",             "name": "Drift"},
]

STATE_FILE = "/opt/agentnet/werewolf_data/game_v2_state.json"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ── LLM call ──

def call_llm(system: str, user: str) -> str:
    import urllib.request
    data = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.85,
        "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"speech": text[:300]}


# ── Minimal system prompt — agent tự reasoning ──

def build_system_prompt(role: str) -> str:
    return f"""You are playing Werewolf, a hidden-role social deduction game with 15 players.
Your role is: {role.upper()}

RULES:
- During DAY DISCUSSION: speak freely, accuse, defend, claim roles, lie if needed.
- During DAY VOTING: you MUST vote for someone. Voting "skip" is ONLY allowed if you truly have no suspicion. Voting skip too often makes the game stall and you may lose.
- During NIGHT: if your role is active, choose a target. If inactive, you are asleep.
- WEREWOLVES: you know your teammates (listed in private info). Coordinate to kill villagers.
- SEER: investigate one player each night. Share findings wisely.
- WITCH: use heal to save the attacked, poison to kill a suspect.
- GUARD: protect one player each night. Cannot protect same target twice in a row.
- HUNTER: when you die, you can shoot one player.
- VILLAGER: find and vote out werewolves.

Play to win. Be decisive. Vote for someone during voting phase.
You only know what your role allows. Lie if it helps your team.
Return valid JSON only."""


# ── Route action ──

PHASE_MAP = {
    "night_guard": "guard_protect",
    "night_werewolf": "werewolf_attack",
    "night_witch": "witch_action",
    "night_seer": "seer_check",
    "day_discussion": "speak",
    "day_voting": "vote",
}

SCHEMAS = {
    "night_guard": '{"target": "player_id", "reason": "..."}',
    "night_werewolf": '{"target": "player_id", "reason": "..."}',
    "night_witch": '{"use_heal": true|false, "use_poison_on": "player_id|null", "reason": "..."}',
    "night_seer": '{"target": "player_id", "reason": "..."}',
    "day_discussion": '{"speech": "...", "claim_role": null, "accuse": [], "defend": [], "suggest_vote": null}',
    "day_voting": '{"target": "player_id|skip", "reason": "..."}',
}


def route_action(state, phase, pid, action):
    from game_loop import apply_guard_action, apply_wolf_attack, apply_witch_action, apply_seer_check, apply_vote, apply_speech
    handlers = {
        "night_guard": apply_guard_action,
        "night_werewolf": apply_wolf_attack,
        "night_witch": apply_witch_action,
        "night_seer": apply_seer_check,
        "day_discussion": apply_speech,
        "day_voting": apply_vote,
    }
    fn = handlers.get(phase)
    return fn(state, pid, action) if fn else {"success": False}


PHASE_ADVANCE = {
    "night_guard": Phase.NIGHT_GUARD_DONE,
    "night_werewolf": Phase.NIGHT_WEREWOLF_DONE,
    "night_witch": Phase.NIGHT_WITCH_DONE,
    "night_seer": Phase.NIGHT_SEER_DONE,
}


# ── Run ──

def run_live_game():
    print(f"\n{'='*60}")
    print(f"🐺 WEREWOLF ARENA v2 — LIVE with DeepSeek")
    print(f"{'='*60}\n")

    # Create fresh game
    state = create_game(AGENT_POOL, game_id=f"wwv2-{int(time.time())}", config=GameConfig())

    print(f"Game: {state.game_id}")
    print(f"Players: {len(state.players)}")
    for p in state.players:
        print(f"  {p.name:18s} → {p.role.value}")
    print()

    phase_count = 0
    llm_calls = 0
    total_cost = 0
    start_time = time.time()

    while not state.game_over and phase_count < 300:
        phase_count += 1
        phase_name = state.phase.value
        print(f"\n── [{phase_count:3d}] {phase_name.upper():25s} (D{state.day} N{state.night}) ──")

        calls = run_game_next_phase(state)
        if not calls:
            print(f"     → auto")
            continue

        phases_seen = set()
        for call in calls:
            pid = call["player_id"]
            obs = call.get("observation", {})
            phase = call.get("phase")
            player = next((p for p in state.players if p.id == pid), p)
            role = obs.get("you", {}).get("role", "villager")

            # Build observation prompt (NO hidden state)
            obs_text = json.dumps(obs, indent=2)
            schema = SCHEMAS.get(phase, "{}")
            user_prompt = f"Current observation:\n{obs_text}\n\nValid JSON schema:\n{schema}\n\nReturn valid JSON only."

            system = build_system_prompt(role)

            t0 = time.time()
            raw = call_llm(system, user_prompt)
            elapsed = time.time() - t0
            llm_calls += 1

            action = extract_json(raw)
            result = route_action(state, phase, pid, action)
            phases_seen.add(phase)

            # Show speech or action summary
            if phase == "day_discussion":
                speech = action.get("speech", "")[:80]
                print(f"     💬 {player.name}: \"{speech}...\"")
            elif phase == "day_voting":
                target = action.get("target", "skip")
                print(f"     🗳️ {player.name} → {target}")
            elif phase == "night_werewolf":
                target = action.get("target", "?")
                print(f"     🐺 {player.name} → {target}")
            elif phase == "night_seer":
                target = action.get("target", "?")
                print(f"     🔮 {player.name} checks {target}")
            elif phase == "night_guard":
                target = action.get("target", "?")
                print(f"     🛡️ {player.name} protects {target}")
            elif phase == "night_witch":
                heal = "Y" if action.get("use_heal") else "N"
                poison = action.get("use_poison_on", "N")
                print(f"     🧙 {player.name}: heal={heal} poison={poison}")

        # Advance phase
        for p in phases_seen:
            nxt = PHASE_ADVANCE.get(p)
            if nxt:
                state.phase = nxt

        # Save state for dashboard
        save_game_state(state, STATE_FILE)

        # Brief pause for readability
        time.sleep(0.3)

    # Game over
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    winner = state.winner or "draw"
    print(f"🏆 {winner.upper()} WINS!")
    print(f"📊 {phase_count} phases, {llm_calls} LLM calls in {elapsed:.0f}s")
    print()
    for p in state.players:
        icon = "✅" if p.alive else "💀"
        print(f"  {icon} {p.name:18s} → {p.role.value}")

    # Save final
    save_game_state(state, STATE_FILE)
    print(f"\nState saved to {STATE_FILE}")

    # Transcript
    transcript_path = os.path.join(os.path.dirname(STATE_FILE), f"transcript_{state.game_id}.json")
    with open(transcript_path, "w") as f:
        json.dump({
            "game_id": state.game_id,
            "winner": winner,
            "duration_s": round(elapsed),
            "llm_calls": llm_calls,
            "players": [{"id": p.id, "name": p.name, "role": p.role.value, "alive": p.alive} for p in state.players],
            "public_history": [{"type": e.type, "content": e.content, "day": e.day} for e in state.public_history],
        }, f, indent=2)
    print(f"Transcript saved to {transcript_path}")
    return state


if __name__ == "__main__":
    if not DEEPSEEK_API_KEY:
        print("❌ DEEPSEEK_API_KEY not set! Add to .env or export.")
        sys.exit(1)

    print(f"DeepSeek key: {DEEPSEEK_API_KEY[:8]}...{DEEPSEEK_API_KEY[-4:]}")
    run_live_game()
