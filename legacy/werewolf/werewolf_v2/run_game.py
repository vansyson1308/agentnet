"""
Werewolf v2 — Game Runner
===========================
High-level game runner. Calls run_game_next_phase() repeatedly
and collects actions, advancing the state machine.
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
    apply_guard_action, apply_wolf_attack, apply_witch_action,
    apply_seer_check, apply_vote, apply_speech,
)


# ── Agent Pool (15 players) ──

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

SYSTEM_PROMPTS = {
    "villager": "You are a Villager in Werewolf. Find and eliminate all werewolves during the day.",
    "werewolf": "You are a Werewolf. Blend in with villagers during the day. Kill villagers at night. Your wolf teammates are listed in your private info.",
    "seer": "You are the Seer. Each night you can investigate one player to learn if they are a werewolf. Share your findings wisely.",
    "witch": "You are the Witch. You have a healing potion (save the attacked player) and a poison potion (kill any player). Use them wisely.",
    "guard": "You are the Guard. Each night you can protect one player from werewolf attack. You cannot protect the same player two nights in a row.",
    "hunter": "You are the Hunter. When you die, you can shoot one player to take them with you.",
}


# ── LLM (DeepSeek) ──

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


def call_llm(system_prompt: str, user_prompt: str, max_retries=2) -> str:
    if not DEEPSEEK_API_KEY:
        return json.dumps({"error": "No API key", "speech": "I pass."})
    import urllib.request
    for attempt in range(max_retries):
        try:
            data = json.dumps({
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 400,
            }).encode("utf-8")
            req = urllib.request.Request(
                DEEPSEEK_API_URL, data=data,
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return json.dumps({"speech": f"I pass. (LLM error: {e})"})
    return json.dumps({"speech": "I pass."})


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
        return {"speech": text[:200], "target": None}


def mock_action(observation: dict) -> dict:
    you = observation.get("you", {})
    phase = observation.get("phase", "day_discussion")

    if "night_guard" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random pick."}
    if "night_werewolf" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random pick."}
    if "night_witch" in phase:
        attacked = observation.get("attacked_player")
        heal = attacked is not None and random.random() < 0.4
        poison = None
        if observation.get("poison_available") and random.random() < 0.15:
            eligible = observation.get("eligible_poison_targets", [])
            if eligible:
                poison = random.choice(eligible)
        return {"use_heal": heal, "use_poison_on": poison, "reason": "Random decision."}
    if "night_seer" in phase:
        eligible = observation.get("eligible_targets", [])
        return {"target": random.choice(eligible) if eligible else None, "reason": "Random check."}
    if "day_discussion" in phase:
        alive = observation.get("alive_players", [])
        target = random.choice(alive) if alive else None
        return {
            "speech": f"I find {target} suspicious based on their behavior.",
            "claim_role": None, "accuse": [target] if target else [],
            "defend": [], "suggest_vote": target,
        }
    if "day_voting" in phase:
        alive = observation.get("alive_players", [])
        return {"target": random.choice(alive) if alive else "skip", "reason": "Random vote."}
    return {"speech": "I pass."}


# ── Agent Action Router ──

def process_action(state: GameState, call: dict, use_llm: bool = False) -> dict:
    phase = call.get("phase")
    pid = call.get("player_id")
    obs = call.get("observation")
    player = next((p for p in state.players if p.id == pid), None)
    if not player:
        return {"success": False, "error": "Player not found"}

    role_name = obs.get("you", {}).get("role", "villager") if obs else "villager"

    if use_llm:
        system = SYSTEM_PROMPTS.get(role_name, "You are playing Werewolf.")
        formatted = format_observation_for_prompt(obs) if obs else "No observation."
        user = f"Current phase: {phase}\n{formatted}\n\nReturn valid JSON only."
        raw = call_llm(system, user)
    else:
        raw = json.dumps(mock_action(obs) if obs else {})

    action = extract_json(raw)

    if phase == "night_guard":
        result = apply_guard_action(state, pid, action)
        return {"success": result.get("success", False), "action": action}
    elif phase == "night_werewolf":
        result = apply_wolf_attack(state, pid, action)
        return {"success": result.get("success", False), "action": action}
    elif phase == "night_witch":
        result = apply_witch_action(state, pid, action)
        return {"success": result.get("success", False), "action": action}
    elif phase == "night_seer":
        result = apply_seer_check(state, pid, action)
        return {"success": result.get("success", False), "action": action, "result": result}
    elif phase == "day_discussion":
        result = apply_speech(state, pid, action)
        return {"success": result.get("success", False), "action": action}
    elif phase == "day_voting":
        result = apply_vote(state, pid, action)
        return {"success": result.get("success", False), "action": action}
    return {"success": False, "error": f"Unknown phase: {phase}"}


# ── Phase Advance Map ──

PHASE_ADVANCE_MAP = {
    "night_guard": "night_guard_done",
    "night_werewolf": "night_werewolf_done",
    "night_witch": "night_witch_done",
    "night_seer": "night_seer_done",
}


def _advance_phase(state: GameState, phase_name: str):
    """Advance phase to its 'done' state so the engine moves to the next."""
    next_phase = PHASE_ADVANCE_MAP.get(phase_name)
    if next_phase:
        state.phase = Phase(next_phase)


def run_game(players=None, use_llm=False, game_id="", state_path="", max_phases=200):
    state_path = state_path or "/opt/agentnet/werewolf_v2/game_state.json"
    players = players or AGENT_POOL[:15]

    state = load_game_state(state_path)
    if not state:
        state = create_game(players, game_id=game_id or f"wwv2-{int(time.time())}")
        print(f"[GAME] New game: {state.game_id} — {len(players)} players")
        for p in state.players:
            print(f"  {p.name:18s} → {p.role.value}")
        save_game_state(state, state_path)

    phase_count = 0
    while not state.game_over and phase_count < max_phases:
        phase_count += 1
        phase_name = state.phase.value
        print(f"\n[{phase_count:3d}] {phase_name:25s} (D{state.day} N{state.night})")

        calls = run_game_next_phase(state)

        if not calls:
            print(f"      auto-transition")
            continue

        phases_processed = set()
        for call in calls:
            pid = call["player_id"]
            player = next((p for p in state.players if p.id == pid), None)
            name = player.name if player else pid
            result = process_action(state, call, use_llm)
            phases_processed.add(call["phase"])

            status = "✅" if result.get("success") else "⚠️"
            action_summary = summarize_action(result.get("action", {}))
            print(f"      {status} {name:18s} → {action_summary}")

        # Advance phase after processing all actions
        for p in phases_processed:
            _advance_phase(state, p)

        save_game_state(state, state_path)

    print(f"\n{'='*50}")
    print(f"🏆 {state.winner.upper()} WINS!" if state.winner else "🏆 DRAW!")
    for p in state.players:
        icon = "✅" if p.alive else "💀"
        print(f"  {icon} {p.name:18s} → {p.role.value}")

    save_game_state(state, state_path)
    return state


def summarize_action(action: dict) -> str:
    if not action:
        return "no action"
    if "speech" in action:
        return f'"{action["speech"][:40]}..."'
    if "target" in action and action["target"]:
        return f"→ {action['target']}"
    if "use_heal" in action:
        parts = []
        if action.get("use_heal"):
            parts.append("heal")
        if action.get("use_poison_on"):
            parts.append(f"poison→{action['use_poison_on']}")
        return " | ".join(parts) if parts else "skip"
    return str(action)[:50]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="Use DeepSeek API")
    parser.add_argument("--players", type=int, default=15)
    parser.add_argument("--game-id", type=str, default="")
    args = parser.parse_args()

    run_game(
        players=AGENT_POOL[:min(args.players, 15)],
        use_llm=args.llm,
        game_id=args.game_id,
    )
