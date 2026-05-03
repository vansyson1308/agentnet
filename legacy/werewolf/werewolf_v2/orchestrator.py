"""
Werewolf v2 — Orchestrator
============================
Drives the game loop for real LLM agents.
Reads game state, calls DeepSeek for each player's action, advances phases.
"""
import sys
import os
import json
import time
import random
import argparse

# Add this dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (
    GameState, GameConfig, Player, Role, Team, Phase,
    build_observation_for_player,
    DAY_SPEAK_SCHEMA, VOTE_SCHEMA, WEREWOLF_ATTACK_SCHEMA,
    SEER_CHECK_SCHEMA, WITCH_ACTION_SCHEMA, GUARD_PROTECT_SCHEMA, HUNTER_SHOT_SCHEMA,
)
from game_loop import (
    create_game, run_game_next_phase, save_game_state, load_game_state,
    format_observation_for_prompt,
    apply_guard_action, apply_wolf_attack, apply_witch_action,
    apply_seer_check, apply_vote, apply_speech,
)

# DeepSeek API
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# AI Agent definitions (AgentNet agents + virtual players)
AGENT_POOL = [
    {"id": "hermes-planner",    "name": "Planner"},
    {"id": "hermes-builder",    "name": "Builder"},
    {"id": "hermes-qaagent",    "name": "QAAgent"},
    {"id": "hermes-storyteller","name": "Storyteller"},
    {"id": "echo",              "name": "Echo"},
    {"id": "poll",              "name": "Poll"},
    {"id": "openclaw",          "name": "OpenClaw"},
    {"id": "hermes-builder-v6", "name": "BuilderV6"},
    # Virtual players
    {"id": "shadow",            "name": "Shadow"},
    {"id": "ember",             "name": "Ember"},
    {"id": "frost",             "name": "Frost"},
    {"id": "blitz",             "name": "Blitz"},
    {"id": "nova",              "name": "Nova"},
    {"id": "vex",               "name": "Vex"},
    {"id": "drift",             "name": "Drift"},
]


def get_system_prompt(role_name: str) -> str:
    """Generate system prompt for an AI agent based on their role."""
    return f"""You are playing a hidden-role social deduction game: Werewolf / Ma Sói.

Your role is: {role_name.upper()}

You must play to win for your team.
- If you are a villager or special village role: your goal is to find and eliminate all werewolves.
- If you are a werewolf: your goal is to kill enough villagers so wolves outnumber non-wolves.

IMPORTANT RULES:
1. You only know information available to your role. Do NOT assume hidden information.
2. During the night, if your role is not active, you are asleep and cannot see anything.
3. You may lie, bluff, accuse, defend, claim a fake role, or hide your true role.
4. Other players may also lie — treat everything as potentially false.
5. Your goal is to maximize your team's chance of winning.
6. Return ONLY valid JSON matching the requested action schema. No other text."""


def call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call DeepSeek API and return raw text response."""
    if not DEEPSEEK_API_KEY:
        return json.dumps({"error": "No API key configured", "speech": "I pass."})

    import urllib.request
    import urllib.error

    data = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }).encode("utf-8")

    req = urllib.request.Request(
        DEEPSEEK_API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[LLM CALL ERROR] {e}")
        return json.dumps({"speech": "I pass for now."})


def extract_json(text: str) -> dict:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    text = text.strip()
    # Try to extract from ```json ... ``` blocks
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"speech": text[:200], "target": None}


def handle_agent_action(state: GameState, agent_call: dict, llm_fn=None) -> dict:
    """
    Process one agent's action. If llm_fn is None, uses mock random action.
    Returns {"success": bool, "result": dict}
    """
    phase = agent_call.get("phase")
    player_id = agent_call.get("player_id")
    observation = agent_call.get("observation")
    schema = agent_call.get("action_schema")

    if not player_id or not observation:
        return {"success": False, "error": "Missing player_id or observation"}

    # Build prompt
    system_prompt = get_system_prompt(observation.get("you", {}).get("role", "villager"))
    formatted_obs = format_observation_for_prompt(observation)
    user_prompt = f"Current observation:\n{json.dumps(observation, indent=2)}\n\nAction schema:\n{json.dumps(schema, indent=2)}\n\nReturn your action as valid JSON only."

    if llm_fn:
        raw_response = llm_fn(system_prompt, user_prompt)
    else:
        raw_response = _mock_action(phase, observation)

    action = extract_json(raw_response)

    # Route to appropriate handler
    handler_results = {
        "night_guard": lambda a: apply_guard_action(state, player_id, a),
        "night_werewolf": lambda a: apply_wolf_attack(state, player_id, a),
        "night_witch": lambda a: apply_witch_action(state, player_id, a),
        "night_seer": lambda a: apply_seer_check(state, player_id, a),
        "day_discussion": lambda a: apply_speech(state, player_id, a),
        "day_voting": lambda a: apply_vote(state, player_id, a),
    }

    handler = handler_results.get(phase)
    if handler:
        result = handler(action)
        return {"success": result.get("success", False), "result": result, "action": action}

    return {"success": False, "error": f"Unknown phase: {phase}"}


def _mock_action(phase: str, observation: dict) -> str:
    """Generate a mock action for testing without LLM."""
    you = observation.get("you", {})
    role = you.get("role", "villager")

    if phase == "night_guard":
        eligible = observation.get("eligible_targets", [])
        target = random.choice(eligible) if eligible else None
        return json.dumps({"target": target, "reason": "Random guard target."})

    elif phase == "night_werewolf":
        eligible = observation.get("eligible_targets", [])
        target = random.choice(eligible) if eligible else None
        return json.dumps({"target": target, "reason": "Random attack target."})

    elif phase == "night_witch":
        attacked = observation.get("attacked_player")
        heal = attacked is not None and random.random() < 0.5
        poison = None
        if observation.get("poison_available", False) and random.random() < 0.2:
            eligible = observation.get("eligible_poison_targets", [])
            poison = random.choice(eligible) if eligible else None
        return json.dumps({
            "use_heal": heal,
            "use_poison_on": poison,
            "reason": "Random witch decision.",
        })

    elif phase == "night_seer":
        eligible = observation.get("eligible_targets", [])
        target = random.choice(eligible) if eligible else None
        return json.dumps({"target": target, "reason": "Random seer check."})

    elif phase == "day_discussion":
        alive = observation.get("alive_players", [])
        return json.dumps({
            "speech": f"I think {random.choice(alive) if alive else 'someone'} seems suspicious.",
            "claim_role": None,
            "accuse": [random.choice(alive)] if alive else [],
            "defend": [],
            "suggest_vote": random.choice(alive) if alive else None,
        })

    elif phase == "day_voting":
        alive = observation.get("alive_players", [])
        target = random.choice(alive) if alive else "skip"
        return json.dumps({"target": target, "reason": "Random vote."})

    return json.dumps({"speech": "I pass."})


def run_game_orchestrator(
    player_list: list[dict] = None,
    use_llm: bool = False,
    game_id: str = "",
    max_phases: int = 200,
    state_path: str = "",
):
    """
    Run a complete game.

    player_list: list of {"id": str, "name": str}
    use_llm: if True, calls DeepSeek API for actions; else uses mock random
    """
    state_path = state_path or "/opt/agentnet/werewolf_v2/game_state.json"
    players = player_list or AGENT_POOL[:15]

    # Load existing or create new
    state = load_game_state(state_path)
    if not state:
        state = create_game(players, game_id=game_id or f"wwv2-{int(time.time())}")
        print(f"[GAME] Created new game {state.game_id} with {len(players)} players")
        for p in state.players:
            print(f"  {p.name:15s} → {p.role.value}")

    llm_fn = call_llm if use_llm else None
    phase_count = 0

    while not state.game_over and phase_count < max_phases:
        phase_count += 1
        phase_name = state.phase.value
        print(f"\n{'='*50}")
        print(f"[Phase {phase_count}] {phase_name.upper()} (Day {state.day}, Night {state.night})")

        agent_calls = run_game_next_phase(state)

        if not agent_calls:
            print(f"  → Auto-transition")
            continue

        print(f"  → {len(agent_calls)} agent(s) need to act")

        for call in agent_calls:
            pid = call["player_id"]
            player = next((p for p in state.players if p.id == pid), None)
            pname = player.name if player else pid
            print(f"  🎯 {pname} ({call['phase']})")

            result = handle_agent_action(state, call, llm_fn)

            if not result.get("success"):
                print(f"     ⚠️  Action failed: {result.get('error', 'unknown')}")

        # Save state after each phase
        save_game_state(state, state_path)

    # Game over
    print(f"\n{'='*50}")
    if state.winner:
        print(f"🏆 {state.winner.upper()} WINS!")
    else:
        print("🏆 DRAW!")

    # Print final state
    for p in state.players:
        status = "✅" if p.alive else "💀"
        print(f"  {status} {p.name:15s} → {p.role.value}")

    save_game_state(state, state_path)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="Use real LLM (DeepSeek)")
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock random agents")
    parser.add_argument("--players", type=int, default=15, help="Number of players (6-15)")
    parser.add_argument("--game-id", type=str, default="", help="Game ID")
    args = parser.parse_args()

    if args.players < 6:
        args.players = 6
    if args.players > 15:
        args.players = 15

    player_list = AGENT_POOL[:args.players]
    state = run_game_orchestrator(
        player_list=player_list,
        use_llm=args.llm,
        game_id=args.game_id,
    )
