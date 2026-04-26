#!/usr/bin/env python3
"""
Werewolf v2 — Cron Game Runner
================================
Runs one complete game (mock mode as fallback).
Saves state to /opt/agentnet/werewolf_v2/game_state.json for dashboard to read.
Role is randomly shuffled every game.
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

# ── Agent Pool ──
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
USE_LLM = bool(DEEPSEEK_API_KEY)


# ── DeepSeek LLM call ──

def call_llm(system_prompt: str, user_prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        return json.dumps({"speech": "I pass. (no API key)"})
    import urllib.request
    try:
        data = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 400,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions", data=data,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return json.dumps({"speech": f"I pass. (error: {e})"})


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
        return {"speech": text[:200]}


def mock_action(observation: dict) -> dict:
    phase = observation.get("phase", "")
    if "night_guard" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random"}
    if "night_werewolf" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random"}
    if "night_witch" in phase:
        attacked = observation.get("attacked_player")
        heal = attacked is not None and random.random() < 0.4
        poison = None
        if observation.get("poison_available") and random.random() < 0.15:
            eligible = observation.get("eligible_poison_targets", [])
            if eligible:
                poison = random.choice(eligible)
        return {"use_heal": heal, "use_poison_on": poison, "reason": "Random"}
    if "night_seer" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random"}
    if "day_discussion" in phase:
        alive = observation.get("alive_players", [])
        target = random.choice(alive) if alive else None
        return {"speech": f"I think {target} seems suspicious.", "claim_role": None,
                "accuse": [target] if target else [], "defend": [], "suggest_vote": target}
    if "day_voting" in phase:
        alive = observation.get("alive_players", [])
        return {"target": random.choice(alive) if alive else "skip", "reason": "Random"}
    return {"speech": "I pass."}


# ── Route action ──

def route_action(state, phase, pid, action):
    from game_loop import apply_guard_action, apply_wolf_attack, apply_witch_action, apply_seer_check, apply_vote, apply_speech
    map = {
        "night_guard": apply_guard_action,
        "night_werewolf": apply_wolf_attack,
        "night_witch": apply_witch_action,
        "night_seer": apply_seer_check,
        "day_discussion": apply_speech,
        "day_voting": apply_vote,
    }
    fn = map.get(phase)
    if fn:
        return fn(state, pid, action)
    return {"success": False}


PHASE_ADVANCE = {
    "night_guard": Phase.NIGHT_GUARD_DONE,
    "night_werewolf": Phase.NIGHT_WEREWOLF_DONE,
    "night_witch": Phase.NIGHT_WITCH_DONE,
    "night_seer": Phase.NIGHT_SEER_DONE,
    "day_voting": Phase.DAY_EXECUTION,
}


def run_game(max_phases=300):
    """Run one complete game."""
    # Create new game
    state = create_game(
        AGENT_POOL,
        game_id=f"wwv2-{int(time.time())}",
        config=GameConfig(),
    )

    print(f"[GAME] Started: {state.game_id} — 15 players")
    for p in state.players:
        print(f"  {p.name:18s} → {p.role.value}")

    phase_count = 0
    while not state.game_over and phase_count < max_phases:
        phase_count += 1
        phase_name = state.phase.value

        calls = run_game_next_phase(state)

        if not calls:
            continue

        phases_seen = set()
        for call in calls:
            pid = call["player_id"]
            obs = call.get("observation", {})
            phase = call.get("phase")

            if USE_LLM:
                role = obs.get("you", {}).get("role", "villager")
                prompts = {
                    "villager": "You are a Villager. Find werewolves during the day.",
                    "werewolf": "You are a Werewolf. Blend in. Kill at night.",
                    "seer": "You are the Seer. Investigate one player each night.",
                    "witch": "You are the Witch. Save or kill with potions.",
                    "guard": "You are the Guard. Protect one player each night.",
                    "hunter": "You are the Hunter. Shoot someone when you die.",
                }
                system = prompts.get(role, "Play Werewolf.")
                formatted = format_observation_for_prompt(obs)
                user = f"Current phase: {phase}\n{formatted}\n\nReturn valid JSON only."
                raw = call_llm(system, user)
            else:
                raw = json.dumps(mock_action(obs))

            action = extract_json(raw)
            result = route_action(state, phase, pid, action)
            phases_seen.add(phase)

        # Advance phase
        for p in phases_seen:
            nxt = PHASE_ADVANCE.get(p)
            if nxt:
                state.phase = nxt

        # Save after each phase
        save_game_state(state, STATE_FILE)

    # Game over
    print(f"\n{'='*50}")
    winner = state.winner or "draw"
    print(f"🏆 {winner.upper()} WINS! ({phase_count} phases)")

    # Print results
    for p in state.players:
        icon = "✅" if p.alive else "💀"
        print(f"  {icon} {p.name:18s} → {p.role.value}")

    save_game_state(state, STATE_FILE)
    print(f"\n[GAME] State saved to {STATE_FILE}")

    # Save transcript
    transcript_path = os.path.join(os.path.dirname(STATE_FILE), f"transcript_{state.game_id}.json")
    with open(transcript_path, "w") as f:
        json.dump({
            "game_id": state.game_id,
            "winner": winner,
            "players": [{"id": p.id, "name": p.name, "role": p.role.value, "alive": p.alive} for p in state.players],
            "public_history": [{"type": e.type, "content": e.content, "day": e.day} for e in state.public_history],
        }, f, indent=2)
    print(f"[GAME] Transcript saved to {transcript_path}")

    return state


if __name__ == "__main__":
    print(f"[GAME] Using {'DeepSeek LLM' if USE_LLM else 'MOCK'} agents")
    start = time.time()
    try:
        state = run_game()
        elapsed = time.time() - start
        print(f"[GAME] Completed in {elapsed:.1f}s")
    except Exception as e:
        print(f"[GAME] CRASHED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
