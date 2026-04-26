"""
Werewolf v2 — Phase Engine (Game Loop)
========================================
State machine that drives the game: setup → night phases → day phases → game over.
Calls build_observation_for_player() for each agent and collects actions.
"""

import json
import random
import os
import time
from typing import Optional
from dataclasses import asdict

from engine import (
    GameState, GameConfig, Player, Role, Team, Phase, PublicEvent, DebugTranscript,
    PrivateMemory, SeerMemory, WitchMemory, WolfMemory, GuardMemory,
    ROLE_DISTRIBUTIONS, TEAM_MAP, STATE_FILE, TRANSCRIPT_FILE,
    build_observation_for_player,
    DAY_SPEAK_SCHEMA, VOTE_SCHEMA, WEREWOLF_ATTACK_SCHEMA,
    SEER_CHECK_SCHEMA, WITCH_ACTION_SCHEMA, GUARD_PROTECT_SCHEMA, HUNTER_SHOT_SCHEMA,
)

__all__ = [
    "create_game", "run_game_next_phase",
    "save_game_state", "load_game_state",
    "format_observation_for_prompt",
]


# ══════════════════════════════════════════════════════════
# Game Initialization
# ══════════════════════════════════════════════════════════

def create_game(
    player_names: list[dict],
    game_id: str = "",
    config: Optional[GameConfig] = None,
    role_list: Optional[list[Role]] = None,
) -> GameState:
    """
    Create a new game.

    player_names: list of {"id": str, "name": str}
    role_list: override role distribution. If None, auto-balance by count.
    """
    n = len(player_names)
    state = GameState()
    state.game_id = game_id or f"werewolf-{int(time.time())}"
    state.config = config or GameConfig()

    # Determine roles
    if role_list:
        roles = list(role_list)
    elif n in ROLE_DISTRIBUTIONS:
        roles = list(ROLE_DISTRIBUTIONS[n])
    else:
        # Auto-balance: ~30% wolves, rest villagers + special roles
        roles = _auto_balance_roles(n)

    # Ensure we have exactly n roles
    if len(roles) != n:
        if len(roles) < n:
            # Pad with villagers
            roles += [Role.VILLAGER] * (n - len(roles))
        else:
            roles = roles[:n]

    random.shuffle(roles)

    # Create players
    for i, pinfo in enumerate(player_names):
        role = roles[i]
        player = Player(
            id=pinfo["id"],
            name=pinfo["name"],
            role=role,
            team=TEAM_MAP[role],
            alive=True,
        )
        state.players.append(player)

    # Initialize private memories
    for p in state.players:
        pm = PrivateMemory()
        if p.role == Role.SEER:
            pm.seer = SeerMemory()
        elif p.role == Role.WITCH:
            pm.witch = WitchMemory()
        elif p.role == Role.WEREWOLF:
            wolf_ids = [pl.id for pl in state.players if pl.role == Role.WEREWOLF]
            pm.wolf = WolfMemory(known_wolves=wolf_ids)
        elif p.role == Role.GUARD:
            pm.guard = GuardMemory()
        state.private_memories[p.id] = pm

    state.public_history.append(PublicEvent(
        type="day_announcement",
        content=f"Game started! {n} players. "
                f"{sum(1 for p in state.players if p.role == Role.WEREWOLF)} werewolves among us."
    ))

    return state


def _auto_balance_roles(n: int) -> list[Role]:
    """Auto-generate role list for an arbitrary player count."""
    if n in ROLE_DISTRIBUTIONS:
        return list(ROLE_DISTRIBUTIONS[n])

    n_wolves = max(2, round(n * 0.3))
    roles = [Role.WEREWOLF] * n_wolves
    roles += [Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER]
    remaining = n - len(roles)
    roles += [Role.VILLAGER] * remaining
    return roles


# ══════════════════════════════════════════════════════════
# Phase Engine — run the next phase
# ══════════════════════════════════════════════════════════

def run_game_next_phase(state: GameState) -> list[dict]:
    """
    Advance the game by one phase.
    Returns a list of agent calls needed: [{player_id, observation, action_schema, phase}].

    After processing agent responses, call apply_agent_action() or advance_phase()
    to update state.

    If returns empty list, phase transitioned automatically (night resolution, game over).
    """
    phase = state.phase

    if phase == Phase.SETUP:
        state.night = 0
        state.phase = Phase.NIGHT_GUARD
        state.public_history.append(PublicEvent(type="day_announcement", content="Night 1 begins. Everyone goes to sleep."))
        return []

    elif phase == Phase.NIGHT_GUARD:
        if _has_alive_role(state, Role.GUARD):
            guard = _get_alive_player(state, Role.GUARD)
            obs = build_observation_for_player(state, guard.id)
            return [{"player_id": guard.id, "observation": obs, "action_schema": GUARD_PROTECT_SCHEMA, "phase": "night_guard"}]
        else:
            state.phase = Phase.NIGHT_WEREWOLF
            return []
    elif phase == Phase.NIGHT_GUARD_DONE:
        state.phase = Phase.NIGHT_WEREWOLF
        return []

    elif phase == Phase.NIGHT_WEREWOLF:
        wolves = [p for p in state.players if p.role == Role.WEREWOLF and p.alive]
        if wolves:
            calls = []
            for wolf in wolves:
                obs = build_observation_for_player(state, wolf.id)
                calls.append({"player_id": wolf.id, "observation": obs, "action_schema": WEREWOLF_ATTACK_SCHEMA, "phase": "night_werewolf"})
            return calls
        else:
            state.phase = Phase.NIGHT_WITCH
            return []
    elif phase == Phase.NIGHT_WEREWOLF_DONE:
        state.phase = Phase.NIGHT_WITCH
        return []

    elif phase == Phase.NIGHT_WITCH:
        if _has_alive_role(state, Role.WITCH):
            witch = _get_alive_player(state, Role.WITCH)
            obs = build_observation_for_player(state, witch.id)
            return [{"player_id": witch.id, "observation": obs, "action_schema": WITCH_ACTION_SCHEMA, "phase": "night_witch"}]
        else:
            state.phase = Phase.NIGHT_SEER
            return []
    elif phase == Phase.NIGHT_WITCH_DONE:
        state.phase = Phase.NIGHT_SEER
        return []

    elif phase == Phase.NIGHT_SEER:
        if _has_alive_role(state, Role.SEER):
            seer = _get_alive_player(state, Role.SEER)
            obs = build_observation_for_player(state, seer.id)
            return [{"player_id": seer.id, "observation": obs, "action_schema": SEER_CHECK_SCHEMA, "phase": "night_seer"}]
        else:
            state.phase = Phase.NIGHT_RESOLUTION
            return []
    elif phase == Phase.NIGHT_SEER_DONE:
        state.phase = Phase.NIGHT_RESOLUTION
        return []

    elif phase == Phase.NIGHT_RESOLUTION:
        _resolve_night(state)
        state.phase = Phase.DAY_ANNOUNCEMENT
        return []

    elif phase == Phase.DAY_ANNOUNCEMENT:
        # Announce night results
        _make_night_announcement(state)
        if state.game_over:
            _announce_game_over(state)
            return []
        state.discussion_round = 0
        state.day_speeches = [s for s in state.day_speeches if s.get("day") != state.day]  # clear old
        state.phase = Phase.DAY_DISCUSSION
        return []

    elif phase == Phase.DAY_DISCUSSION:
        alive = [p for p in state.players if p.alive]
        if state.discussion_round < state.config.discussion_rounds_per_day:
            calls = []
            # Only living players speak
            for player in alive:
                obs = build_observation_for_player(state, player.id)
                calls.append({"player_id": player.id, "observation": obs, "action_schema": DAY_SPEAK_SCHEMA, "phase": "day_discussion"})
            state.discussion_round += 1
            return calls
        else:
            # Move to voting
            state.discussion_round = 0
            state.day_votes = {}
            state.phase = Phase.DAY_VOTING
            return []

    elif phase == Phase.DAY_VOTING:
        alive = [p for p in state.players if p.alive]
        calls = []
        for player in alive:
            obs = build_observation_for_player(state, player.id)
            calls.append({"player_id": player.id, "observation": obs, "action_schema": VOTE_SCHEMA, "phase": "day_voting"})
        return calls

    elif phase == Phase.DAY_EXECUTION:
        _resolve_execution(state)
        if state.game_over:
            _announce_game_over(state)
            return []
        # Hunter might get a chance
        state.phase = Phase.HUNTER_SHOT
        return []

    elif phase == Phase.HUNTER_SHOT:
        # Check if any dead player was Hunter and can shoot
        if _check_hunter_shot(state):
            return []  # handled internally
        # Advance to next night
        return _advance_to_night(state)

    elif phase == Phase.GAME_OVER:
        return []

    return []


# ══════════════════════════════════════════════════════════
# Agent Action Application
# ══════════════════════════════════════════════════════════

def apply_guard_action(state: GameState, player_id: str, action: dict) -> dict:
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or player.role != Role.GUARD:
        return {"error": "Not a guard", "success": False}

    target = action.get("target")
    if not target:
        return {"error": "No target specified", "success": False}

    # Validate: alive
    target_player = next((p for p in state.players if p.id == target), None)
    if not target_player or not target_player.alive:
        return {"error": "Target not alive", "success": False}

    # Validate: not self if config forbids
    if target == player_id and not state.config.guard_can_self_protect:
        return {"error": "Cannot protect self", "success": False}

    # Validate: not same as last night
    if state.config.guard_cannot_protect_same_target_consecutively:
        if state.guard_previous_target and target == state.guard_previous_target:
            return {"error": "Cannot protect same player two nights in a row", "success": False}

    state.guard_target = target
    state.guard_previous_target = target

    # Save to memory
    pm = state.private_memories.get(player_id)
    if pm and pm.guard:
        pm.guard.protected_history.append({"night": state.night, "target": target})

    return {"success": True}


def apply_wolf_attack(state: GameState, player_id: str, action: dict) -> dict:
    """Record a wolf's vote. After all wolves vote, resolve majority."""
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or player.role != Role.WEREWOLF:
        return {"error": "Not a werewolf", "success": False}

    target = action.get("target")
    if not target:
        return {"error": "No target specified", "success": False}

    # Must be alive non-wolf
    target_player = next((p for p in state.players if p.id == target), None)
    if not target_player or not target_player.alive or target_player.team == Team.WEREWOLF:
        return {"error": "Cannot attack wolves or dead players", "success": False}

    if target not in state.wolf_targets:
        state.wolf_targets.append(target)

    return {"success": True}


def apply_witch_action(state: GameState, player_id: str, action: dict) -> dict:
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or player.role != Role.WITCH:
        return {"error": "Not a witch", "success": False}

    pm = state.private_memories.get(player_id)
    witch_mem = pm.witch if pm else None
    if not witch_mem:
        return {"error": "No witch memory", "success": False}

    use_heal = action.get("use_heal", False)
    use_poison = action.get("use_poison_on")

    if use_heal:
        if not witch_mem.heal_available:
            return {"error": "Heal potion already used", "success": False}
        if not state.wolf_attack_target:
            return {"error": "No one was attacked tonight", "success": False}
        state.witch_heal_used = True
        witch_mem.heal_available = False

    if use_poison:
        if not witch_mem.poison_available:
            return {"error": "Poison potion already used", "success": False}
        target = next((p for p in state.players if p.id == use_poison), None)
        if not target or not target.alive:
            return {"error": "Poison target not alive", "success": False}
        state.witch_poison_target = use_poison
        witch_mem.poison_available = False

    return {"success": True}


def apply_seer_check(state: GameState, player_id: str, action: dict) -> dict:
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or player.role != Role.SEER:
        return {"error": "Not a seer", "success": False}

    target = action.get("target")
    if not target:
        return {"error": "No target specified", "success": False}

    target_player = next((p for p in state.players if p.id == target), None)
    if not target_player or not target_player.alive:
        return {"error": "Target not alive", "success": False}

    result = "WEREWOLF" if target_player.team == Team.WEREWOLF else "NOT_WEREWOLF"
    state.seer_check_target = target

    # Save to memory
    pm = state.private_memories.get(player_id)
    if pm and pm.seer:
        pm.seer.checks.append({"night": state.night, "target": target, "result": result})

    return {"success": True, "result": result}


def apply_vote(state: GameState, player_id: str, action: dict) -> dict:
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or not player.alive:
        return {"error": "Not alive", "success": False}

    target = action.get("target")
    if target and target != "skip":
        target_player = next((p for p in state.players if p.id == target), None)
        if not target_player or not target_player.alive:
            return {"error": "Vote target not alive", "success": False}

    state.day_votes[player_id] = target or "skip"
    return {"success": True}


def apply_speech(state: GameState, player_id: str, action: dict) -> dict:
    player = next((p for p in state.players if p.id == player_id), None)
    if not player or not player.alive:
        return {"error": "Not alive", "success": False}

    speech = action.get("speech", "")
    if not speech.strip():
        return {"error": "Empty speech", "success": False}

    state.day_speeches.append({
        "day": state.day,
        "round": state.discussion_round,
        "speaker_id": player_id,
        "speaker_name": player.name,
        "speech": speech,
        "claim_role": action.get("claim_role"),
        "accuse": action.get("accuse", []),
        "defend": action.get("defend", []),
        "suggest_vote": action.get("suggest_vote"),
    })

    return {"success": True}


# ══════════════════════════════════════════════════════════
# Night Resolution
# ══════════════════════════════════════════════════════════

def _resolve_night(state: GameState):
    """Resolve all night actions and determine deaths."""
    # Resolve wolf attack
    if state.wolf_targets:
        # Majority vote
        from collections import Counter
        counts = Counter(state.wolf_targets)
        target = counts.most_common(1)[0][0]
        state.wolf_attack_target = target
    else:
        state.wolf_attack_target = None

    deaths = []

    # Check wolf attack
    wolf_killed = None
    if state.wolf_attack_target:
        is_protected = state.guard_target == state.wolf_attack_target
        is_healed = state.witch_heal_used

        if not is_protected and not is_healed:
            wolf_killed = state.wolf_attack_target
            deaths.append(wolf_killed)

    # Check witch poison
    if state.witch_poison_target:
        if state.witch_poison_target not in deaths:
            deaths.append(state.witch_poison_target)

    # Apply deaths
    for pid in deaths:
        player = next((p for p in state.players if p.id == pid), None)
        if player:
            player.alive = False

    # Build debug transcript
    debug = DebugTranscript(
        night=state.night,
        wolves_target=state.wolf_attack_target,
        guard_protected=state.guard_target,
        witch_healed=state.witch_heal_used,
        witch_poisoned=state.witch_poison_target,
        seer_check={"seer": _get_seer_id(state), "target": state.seer_check_target,
                     "result": _get_seer_result(state)} if state.seer_check_target else None,
    )
    state.debug_transcript.append(debug)

    # Save night actions to wolf memory
    for p in state.players:
        if p.role == Role.WEREWOLF and p.alive:
            pm = state.private_memories.get(p.id)
            if pm and pm.wolf and state.wolf_attack_target:
                pm.wolf.attack_history.append({"night": state.night, "target": state.wolf_attack_target})

    # Save night attack to witch memory
    witch = _get_alive_player(state, Role.WITCH)
    if witch and state.wolf_attack_target:
        pm = state.private_memories.get(witch.id)
        if pm and pm.witch:
            pm.witch.nights_seen_attacks.append({"night": state.night, "attacked": state.wolf_attack_target})

    # Reset night state
    state.guard_target = None
    state.wolf_targets = []
    state.witch_heal_used = False
    state.witch_poison_target = None
    state.seer_check_target = None

    # Check win condition
    _check_win_condition(state)


def _make_night_announcement(state: GameState):
    """Public morning announcement of night's events."""
    dead = [p for p in state.players if not p.alive]
    just_died = [p for p in dead if not _is_in_death_log(state, p.id, state.night)]

    if just_died:
        names = ", ".join(p.name for p in just_died)
        state.public_history.append(PublicEvent(
            type="night_result",
            content=f"Last night, {names} died.",
            deaths=[p.id for p in just_died],
        ))
    else:
        state.public_history.append(PublicEvent(
            type="night_result",
            content="No one died last night.",
        ))


def _is_in_death_log(state: GameState, player_id: str, night: int) -> bool:
    """Check if player died this night (approximate)."""
    event = next((e for e in reversed(state.public_history) if e.type == "night_result"), None)
    return event and player_id in event.deaths


def _check_win_condition(state: GameState):
    alive_wolves = sum(1 for p in state.players if p.alive and p.team == Team.WEREWOLF)
    alive_villagers = sum(1 for p in state.players if p.alive and p.team == Team.VILLAGE)

    if alive_wolves == 0:
        state.game_over = True
        state.winner = "village"
        state.public_history.append(PublicEvent(type="game_over", winner="village", content="Village wins! All werewolves have been eliminated."))
    elif alive_wolves >= alive_villagers:
        state.game_over = True
        state.winner = "werewolves"
        state.public_history.append(PublicEvent(type="game_over", winner="werewolves", content="Werewolves win! They have taken over the village."))

    if state.night >= state.config.max_rounds:
        state.game_over = True
        state.winner = "draw"
        state.public_history.append(PublicEvent(type="game_over", winner="draw", content="Draw! Maximum rounds reached."))


def _announce_game_over(state: GameState):
    pass  # already in public_history


def _check_hunter_shot(state: GameState) -> bool:
    """Check if a Hunter died and needs to shoot. Returns True if handled."""
    # This is checked before advancing — for MVP, hunter shot is optional
    return False


def _advance_to_night(state: GameState) -> list[dict]:
    """Move to next night phase."""
    if state.game_over:
        return []

    state.night += 1
    state.day += 1
    state.public_history.append(PublicEvent(type="day_announcement", content=f"Night {state.night} begins. Everyone goes to sleep."))
    state.phase = Phase.NIGHT_GUARD
    return _run_transition(state)


def _run_transition(state: GameState) -> list[dict]:
    """Transition helper."""
    return run_game_next_phase(state)


def _has_alive_role(state: GameState, role: Role) -> bool:
    return any(p.alive and p.role == role for p in state.players)


def _get_alive_player(state: GameState, role: Role) -> Optional[Player]:
    for p in state.players:
        if p.alive and p.role == role:
            return p
    return None


def _get_seer_id(state: GameState) -> Optional[str]:
    for p in state.players:
        if p.role == Role.SEER:
            return p.id
    return None


def _get_seer_result(state: GameState) -> Optional[str]:
    if not state.seer_check_target:
        return None
    target = next((p for p in state.players if p.id == state.seer_check_target), None)
    if not target:
        return None
    return "WEREWOLF" if target.team == Team.WEREWOLF else "NOT_WEREWOLF"


# ══════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════

def _resolve_execution(state: GameState):
    """Resolve day voting results with auto-execute fallback."""
    if not state.day_votes:
        state.public_history.append(PublicEvent(type="execution", content="No votes were cast."))
        # Auto-execute: pick random alive player if everyone skips
        alive = [p for p in state.players if p.alive]
        if alive:
            target = random.choice(alive)
            target.alive = False
            state.public_history.append(PublicEvent(
                type="execution",
                content=f"{target.name} was randomly executed (no votes).",
                executed=target.id,
            ))
        _check_win_condition(state)
        return

    # Count votes
    from collections import Counter
    vote_counts = Counter(state.day_votes.values())

    # Remove skip
    if "skip" in vote_counts:
        del vote_counts["skip"]

    if not vote_counts:
        state.public_history.append(PublicEvent(type="execution", content="Everyone voted to skip."))
        return

    top_votes = vote_counts.most_common()
    max_count = top_votes[0][1]

    # Check for tie
    tied = [pid for pid, count in top_votes if count == max_count]

    if len(tied) > 1:
        if state.config.tie_vote_policy == "no_execution":
            state.public_history.append(PublicEvent(type="execution", content=f"Vote resulted in a tie ({', '.join(tied)}). No one was executed."))
            return
        elif state.config.tie_vote_policy == "random":
            executed = random.choice(tied)
        else:
            executed = tied[0]
    else:
        executed = tied[0]

    # Execute
    player = next((p for p in state.players if p.id == executed), None)
    if player:
        player.alive = False

    # Public event
    death_content = f"{player.name} was executed."
    state.public_history.append(PublicEvent(
        type="execution",
        content=death_content,
        votes=dict(state.day_votes),
        executed=executed,
    ))

    _check_win_condition(state)


# ══════════════════════════════════════════════════════════
# Format observation for LLM prompt
# ══════════════════════════════════════════════════════════

def format_observation_for_prompt(observation: dict) -> str:
    """Format observation as a human-readable prompt for the AI agent."""
    obs = observation
    lines = []

    # Basic info
    you = obs.get("you", {})
    lines.append(f"You are {you.get('name', '?')} (ID: {you.get('id', '?')}).")
    lines.append(f"Your role: {you.get('role', '?').upper()}")
    lines.append(f"Phase: {obs.get('phase', '?').replace('_', ' ').title()}")
    lines.append(f"Day {obs.get('day', 0)} Night {obs.get('night', 0)}")

    # Alive/dead
    alive = obs.get("alive_players", [])
    dead = obs.get("dead_players", [])
    lines.append(f"Alive players ({len(alive)}): {', '.join(alive)}")
    if dead:
        lines.append(f"Dead players: {', '.join(dead)}")

    # Message
    msg = obs.get("message", "")
    if msg:
        lines.append(f"\n{msg}")

    # Private info
    private_info = obs.get("private_info", [])
    if private_info:
        lines.append("\nYour private information:")
        for info in private_info:
            lines.append(f"  • {info}")

    # Eligible targets
    eligible = obs.get("eligible_targets", [])
    if eligible:
        lines.append(f"\nYou can choose from: {', '.join(eligible)}")

    # Phase-specific
    if obs.get("phase") == "day_discussion":
        speeches = obs.get("speeches_this_day", [])
        if speeches:
            lines.append(f"\nSpeeches this day (round {obs.get('discussion_round', 1)}):")
            for s in speeches:
                lines.append(f"  [{s.get('speaker_name', s.get('speaker_id', '?'))}]: {s.get('speech', '')}")

        lines.append("\nFormat your response as JSON:")
        lines.append('{"speech": "...", "claim_role": null, "accuse": [], "defend": [], "suggest_vote": null}')

    elif obs.get("phase") == "day_voting":
        lines.append("\nFormat your response as JSON:")
        lines.append('{"target": "player_id", "reason": "..."}')

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# Save / Load
# ══════════════════════════════════════════════════════════

def save_game_state(state: GameState, path: str = ""):
    """Save full game state to JSON (includes private info — for engine only)."""
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Build a dict with all private state persisted
    data = {
        "game_id": state.game_id,
        "phase": state.phase.value,
        "day": state.day,
        "night": state.night,
        "game_over": state.game_over,
        "winner": state.winner,
        "players": [p.to_dict() for p in state.players],
        "config": {
            "reveal_role_on_death": state.config.reveal_role_on_death,
            "allow_skip_vote": state.config.allow_skip_vote,
            "tie_vote_policy": state.config.tie_vote_policy,
        },
        "night_state": {
            "guard_target": state.guard_target,
            "guard_previous_target": state.guard_previous_target,
            "wolf_targets": state.wolf_targets,
            "wolf_attack_target": state.wolf_attack_target,
            "witch_heal_used": state.witch_heal_used,
            "witch_poison_target": state.witch_poison_target,
            "seer_check_target": state.seer_check_target,
        },
        "private_memories": _serialize_private_memories(state),
        "public_history": [asdict(e) if hasattr(e, '__dataclass_fields__') else e for e in state.public_history],
        "debug_transcript": [asdict(d) for d in state.debug_transcript],
        "day_speeches": state.day_speeches,
        "day_votes": state.day_votes,
        "discussion_round": state.discussion_round,
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_game_state(path: str = "") -> Optional[GameState]:
    """Restore full game state from JSON."""
    path = path or STATE_FILE
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)

    config = GameConfig(
        reveal_role_on_death=data.get("config", {}).get("reveal_role_on_death", False),
        allow_skip_vote=data.get("config", {}).get("allow_skip_vote", True),
        tie_vote_policy=data.get("config", {}).get("tie_vote_policy", "no_execution"),
    )
    state = GameState(config=config)
    state.game_id = data.get("game_id", "")
    state.phase = Phase(data.get("phase", "setup"))
    state.day = data.get("day", 0)
    state.night = data.get("night", 0)
    state.game_over = data.get("game_over", False)
    state.winner = data.get("winner")
    state.players = [Player.from_dict(p) for p in data.get("players", [])]

    ns = data.get("night_state", {})
    state.guard_target = ns.get("guard_target")
    state.guard_previous_target = ns.get("guard_previous_target")
    state.wolf_targets = ns.get("wolf_targets", [])
    state.wolf_attack_target = ns.get("wolf_attack_target")
    state.witch_heal_used = ns.get("witch_heal_used", False)
    state.witch_poison_target = ns.get("witch_poison_target")
    state.seer_check_target = ns.get("seer_check_target")

    _deserialize_private_memories(state, data.get("private_memories", {}))
    state.public_history = [PublicEvent(**e) for e in data.get("public_history", [])]
    state.debug_transcript = [DebugTranscript(**d) for d in data.get("debug_transcript", [])]
    state.day_speeches = data.get("day_speeches", [])
    state.day_votes = data.get("day_votes", {})
    state.discussion_round = data.get("discussion_round", 0)

    return state


def _serialize_private_memories(state: GameState) -> dict:
    result = {}
    for pid, pm in state.private_memories.items():
        entry = {}
        if pm.seer:
            entry["seer"] = {"checks": pm.seer.checks}
        if pm.witch:
            entry["witch"] = {
                "heal_available": pm.witch.heal_available,
                "poison_available": pm.witch.poison_available,
                "nights_seen_attacks": pm.witch.nights_seen_attacks,
            }
        if pm.wolf:
            entry["wolf"] = {
                "known_wolves": pm.wolf.known_wolves,
                "attack_history": pm.wolf.attack_history,
            }
        if pm.guard:
            entry["guard"] = {"protected_history": pm.guard.protected_history}
        result[pid] = entry
    return result


def _deserialize_private_memories(state: GameState, data: dict):
    for pid, entry in data.items():
        pm = PrivateMemory()
        if "seer" in entry:
            pm.seer = SeerMemory(checks=entry["seer"].get("checks", []))
        if "witch" in entry:
            pm.witch = WitchMemory(
                heal_available=entry["witch"].get("heal_available", True),
                poison_available=entry["witch"].get("poison_available", True),
                nights_seen_attacks=entry["witch"].get("nights_seen_attacks", []),
            )
        if "wolf" in entry:
            pm.wolf = WolfMemory(
                known_wolves=entry["wolf"].get("known_wolves", []),
                attack_history=entry["wolf"].get("attack_history", []),
            )
        if "guard" in entry:
            pm.guard = GuardMemory(protected_history=entry["guard"].get("protected_history", []))
        state.private_memories[pid] = pm
