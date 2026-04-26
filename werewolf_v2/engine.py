"""
Werewolf Game Engine v2 — Information-Isolated Social Deduction
================================================================
Pure logic, no I/O. Every function is testable.
Core principle: Each player sees ONLY what their role permits.
"""

from __future__ import annotations
import json
import random
import os
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── File paths (configurable via env) ──
STATE_FILE = os.environ.get("WWV2_STATE_FILE", "/opt/agentnet/werewolf_v2/game_state.json")
TRANSCRIPT_FILE = os.environ.get("WWV2_TRANSCRIPT_FILE", "/opt/agentnet/werewolf_v2/transcript.json")


# ══════════════════════════════════════════════════════════
# 1. Core Enums & Data Models
# ══════════════════════════════════════════════════════════

class Team(str, Enum):
    VILLAGE = "village"
    WEREWOLF = "werewolf"


class Role(str, Enum):
    VILLAGER = "villager"
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    GUARD = "guard"
    HUNTER = "hunter"


class Phase(str, Enum):
    SETUP = "setup"
    NIGHT_GUARD = "night_guard"
    NIGHT_GUARD_DONE = "night_guard_done"
    NIGHT_WEREWOLF = "night_werewolf"
    NIGHT_WEREWOLF_DONE = "night_werewolf_done"
    NIGHT_WITCH = "night_witch"
    NIGHT_WITCH_DONE = "night_witch_done"
    NIGHT_SEER = "night_seer"
    NIGHT_SEER_DONE = "night_seer_done"
    NIGHT_RESOLUTION = "night_resolution"
    DAY_ANNOUNCEMENT = "day_announcement"
    DAY_DISCUSSION = "day_discussion"
    DAY_VOTING = "day_voting"
    DAY_EXECUTION = "day_execution"
    HUNTER_SHOT = "hunter_shot"
    GAME_OVER = "game_over"


@dataclass
class Player:
    id: str
    name: str
    role: Role
    team: Team
    alive: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role.value, "team": self.team.value, "alive": self.alive}

    @classmethod
    def from_dict(cls, d: dict) -> Player:
        return cls(id=d["id"], name=d["name"], role=Role(d["role"]), team=Team(d["team"]), alive=d.get("alive", True))


@dataclass
class GameConfig:
    """All configurable rules. MVP defaults."""
    reveal_role_on_death: bool = False
    allow_skip_vote: bool = True
    tie_vote_policy: str = "no_execution"  # no_execution | revote | random
    witch_can_self_heal: bool = True
    witch_can_use_both_potions_same_night: bool = True
    guard_can_self_protect: bool = True
    guard_cannot_protect_same_target_consecutively: bool = True
    hunter_can_shoot_when_killed_by_wolves: bool = True
    hunter_can_shoot_when_executed: bool = True
    hunter_can_shoot_when_poisoned: bool = False
    discussion_rounds_per_day: int = 2
    max_speech_tokens: int = 300
    max_rounds: int = 15
    role_setup: Optional[list[Role]] = None  # If set, use these roles instead of auto-balancing


# ══════════════════════════════════════════════════════════
# Role distribution for player counts
# ══════════════════════════════════════════════════════════

ROLE_DISTRIBUTIONS = {
    6:  [Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.VILLAGER, Role.VILLAGER],
    7:  [Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.VILLAGER, Role.VILLAGER],
    8:  [Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER],
    9:  [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER],
    10: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    11: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    12: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    13: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    14: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    15: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
}

TEAM_MAP = {
    Role.VILLAGER: Team.VILLAGE,
    Role.SEER: Team.VILLAGE,
    Role.WITCH: Team.VILLAGE,
    Role.GUARD: Team.VILLAGE,
    Role.HUNTER: Team.VILLAGE,
    Role.WEREWOLF: Team.WEREWOLF,
}


# ══════════════════════════════════════════════════════════
# 2. Private Memory — per-player persistent knowledge
# ══════════════════════════════════════════════════════════

@dataclass
class SeerMemory:
    checks: list[dict] = field(default_factory=list)  # [{night, target, result}]

@dataclass
class WitchMemory:
    heal_available: bool = True
    poison_available: bool = True
    nights_seen_attacks: list[dict] = field(default_factory=list)  # [{night, attacked}]

@dataclass
class WolfMemory:
    known_wolves: list[str] = field(default_factory=list)
    attack_history: list[dict] = field(default_factory=list)  # [{night, target}]

@dataclass
class GuardMemory:
    protected_history: list[dict] = field(default_factory=list)  # [{night, target}]

@dataclass
class PrivateMemory:
    seer: Optional[SeerMemory] = None
    witch: Optional[WitchMemory] = None
    wolf: Optional[WolfMemory] = None
    guard: Optional[GuardMemory] = None


# ══════════════════════════════════════════════════════════
# 3. Public & Private Events
# ══════════════════════════════════════════════════════════

@dataclass
class PublicEvent:
    type: str  # day_speech | vote_result | night_result | day_announcement | execution | game_over
    day: int = 0
    speaker: Optional[str] = None
    content: str = ""
    votes: Optional[dict] = None
    executed: Optional[str] = None
    deaths: list[str] = field(default_factory=list)
    winner: Optional[str] = None


@dataclass
class DebugTranscript:
    """Private debug-only transcript. NEVER sent to agents."""
    night: int = 0
    wolves_target: Optional[str] = None
    guard_protected: Optional[str] = None
    witch_healed: bool = False
    witch_poisoned: Optional[str] = None
    seer_check: Optional[dict] = None  # {seer, target, result}


# ══════════════════════════════════════════════════════════
# 4. Action Schemas
# ══════════════════════════════════════════════════════════

# These define what agents can return in each phase.
# Observation builders filter what the agent sees; action schemas define valid responses.

DAY_SPEAK_SCHEMA = {
    "type": "object",
    "properties": {
        "speech": {"type": "string"},
        "claim_role": {"type": ["string", "null"], "enum": ["villager", "seer", "witch", "guard", "hunter", "werewolf", None]},
        "accuse": {"type": "array", "items": {"type": "string"}},
        "defend": {"type": "array", "items": {"type": "string"}},
        "suggest_vote": {"type": ["string", "null"]},
    },
    "required": ["speech"],
}

VOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": ["string", "null"]},  # null = skip
        "reason": {"type": "string"},
    },
    "required": ["target", "reason"],
}

WEREWOLF_ATTACK_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["target", "reason"],
}

SEER_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["target", "reason"],
}

WITCH_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "use_heal": {"type": "boolean"},
        "use_poison_on": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["use_heal", "use_poison_on", "reason"],
}

GUARD_PROTECT_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["target", "reason"],
}

HUNTER_SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["target", "reason"],
}


# ══════════════════════════════════════════════════════════
# 5. Full Game State
# ══════════════════════════════════════════════════════════

@dataclass
class GameState:
    # Meta
    game_id: str = ""
    phase: Phase = Phase.SETUP
    day: int = 0
    night: int = 0
    config: GameConfig = field(default_factory=GameConfig)
    game_over: bool = False
    winner: Optional[str] = None

    # Players
    players: list[Player] = field(default_factory=list)

    # Night action storage (engine-internal, NEVER sent to agents)
    guard_target: Optional[str] = None
    guard_previous_target: Optional[str] = None
    wolf_targets: list[str] = field(default_factory=list)  # each wolf's vote
    wolf_attack_target: Optional[str] = None
    witch_heal_used: bool = False  # on this night
    witch_poison_target: Optional[str] = None
    seer_check_target: Optional[str] = None

    # Per-player private memory
    private_memories: dict[str, PrivateMemory] = field(default_factory=dict)

    # Public history
    public_history: list[PublicEvent] = field(default_factory=list)

    # Debug-only transcript
    debug_transcript: list[DebugTranscript] = field(default_factory=list)

    # Storage for day discussion speeches (per day)
    day_speeches: list[dict] = field(default_factory=list)

    # Vote tracking
    day_votes: dict[str, str] = field(default_factory=dict)  # voter_id -> target_id

    # Current discussion round
    discussion_round: int = 0

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "phase": self.phase.value,
            "day": self.day,
            "night": self.night,
            "game_over": self.game_over,
            "winner": self.winner,
            "players": [p.to_dict() for p in self.players],
            "public_history": [asdict(e) for e in self.public_history],
            "day_speeches": self.day_speeches,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GameState:
        state = cls()
        state.game_id = d.get("game_id", "")
        state.phase = Phase(d.get("phase", "setup"))
        state.day = d.get("day", 0)
        state.night = d.get("night", 0)
        state.game_over = d.get("game_over", False)
        state.winner = d.get("winner")
        state.players = [Player.from_dict(p) for p in d.get("players", [])]
        state.public_history = [PublicEvent(**e) if isinstance(e, dict) else e for e in d.get("public_history", [])]
        state.day_speeches = d.get("day_speeches", [])
        return state


# ══════════════════════════════════════════════════════════
# 6. Player Observation — WHAT EACH PLAYER SEES
# ══════════════════════════════════════════════════════════

def build_observation_for_player(state: GameState, player_id: str) -> dict:
    """
    THE most important function in this engine.
    Generates a filtered view of the game for a specific player.
    Absolutely NO hidden information leakage.
    """
    player = next((p for p in state.players if p.id == player_id), None)
    if not player:
        return {"error": "Player not found"}

    obs = {
        "you": {
            "id": player.id,
            "name": player.name,
            "role": player.role.value,
            "alive": player.alive,
        },
        "phase": state.phase.value,
        "day": state.day,
        "night": state.night,
        "alive_players": [p.id for p in state.players if p.alive],
        "dead_players": [p.id for p in state.players if not p.alive],
        "public_history": _build_public_history(state),
        "private_info": _build_private_info(state, player),
    }

    # Phase-specific additions
    phase = state.phase

    if phase in (Phase.NIGHT_GUARD, Phase.NIGHT_WEREWOLF, Phase.NIGHT_WITCH,
                 Phase.NIGHT_SEER, Phase.NIGHT_RESOLUTION):
        _add_night_observation(obs, state, player)

    if phase == Phase.DAY_DISCUSSION:
        _add_discussion_observation(obs, state, player)

    if phase == Phase.DAY_VOTING:
        _add_voting_observation(obs, state, player)

    if phase == Phase.DAY_EXECUTION:
        _add_execution_observation(obs, state, player)

    if phase == Phase.HUNTER_SHOT:
        _add_hunter_shot_observation(obs, state, player)

    if phase == Phase.GAME_OVER:
        obs["winner"] = state.winner

    return obs


def _build_public_history(state: GameState) -> list[dict]:
    """Convert PublicEvent list to serializable dicts."""
    return [asdict(e) for e in state.public_history]


def _build_private_info(state: GameState, player: Player) -> list[str]:
    """Build private info strings the player has accumulated."""
    info = []
    pm = state.private_memories.get(player.id)

    if player.role == Role.SEER and pm and pm.seer:
        for check in pm.seer.checks:
            target_name = _get_player_name(state, check["target"])
            result = "WEREWOLF" if check["result"] == "WEREWOLF" else "NOT WEREWOLF"
            info.append(f"Night {check['night']}: You checked {target_name}. Result: {result}.")

    if player.role == Role.WITCH and pm and pm.witch:
        for attack in pm.witch.nights_seen_attacks:
            target_name = _get_player_name(state, attack["attacked"])
            info.append(f"Night {attack['night']}: {target_name} was attacked by wolves.")

    if player.role == Role.WEREWOLF and pm and pm.wolf:
        names = [_get_player_name(state, wid) for wid in pm.wolf.known_wolves if wid != player.id]
        if names:
            info.append(f"Your werewolf teammate{'s are' if len(names) > 1 else ' is'}: {', '.join(names)}.")
        for attack in pm.wolf.attack_history:
            target_name = _get_player_name(state, attack["target"])
            info.append(f"Night {attack['night']}: Pack attacked {target_name}.")

    if player.role == Role.GUARD and pm and pm.guard:
        for prot in pm.guard.protected_history:
            target_name = _get_player_name(state, prot["target"])
            info.append(f"Night {prot['night']}: You protected {target_name}.")

    return info


def _get_player_name(state: GameState, player_id: str) -> str:
    for p in state.players:
        if p.id == player_id:
            return p.name
    return player_id


def _add_night_observation(obs: dict, state: GameState, player: Player):
    """Night phase — only active role sees details; others are 'asleep'."""
    phase = state.phase

    if not player.alive:
        obs["message"] = "You are dead. You cannot act."
        return

    # Guard phase
    if phase == Phase.NIGHT_GUARD:
        if player.role == Role.GUARD:
            eligible = [p.id for p in state.players if p.alive]
            prev = state.guard_previous_target
            if state.config.guard_cannot_protect_same_target_consecutively and prev:
                eligible = [pid for pid in eligible if pid != prev]
            if not state.config.guard_can_self_protect:
                eligible = [pid for pid in eligible if pid != player.id]
            obs["message"] = "Guard wakes up. Choose one player to protect tonight."
            obs["eligible_targets"] = eligible
        else:
            obs["message"] = "Night falls. You are asleep. You cannot act now."
            obs["asleep"] = True

    # Werewolf phase
    elif phase == Phase.NIGHT_WEREWOLF:
        if player.role == Role.WEREWOLF and player.alive:
            eligible = [p.id for p in state.players if p.alive and p.team != Team.WEREWOLF]
            obs["message"] = "Werewolves wake up. Choose one target to attack."
            obs["eligible_targets"] = eligible
        else:
            obs["message"] = "Night falls. You are asleep. You cannot act now."
            obs["asleep"] = True

    # Witch phase
    elif phase == Phase.NIGHT_WITCH:
        if player.role == Role.WITCH and player.alive:
            pm = state.private_memories.get(player.id)
            witch_mem = pm.witch if pm else None
            obs["message"] = "Witch wakes up."
            if state.wolf_attack_target and (not state.guard_target or state.guard_target != state.wolf_attack_target):
                obs["attacked_player"] = state.wolf_attack_target
            obs["heal_available"] = (witch_mem and witch_mem.heal_available) if witch_mem else True
            obs["poison_available"] = (witch_mem and witch_mem.poison_available) if witch_mem else True
            if obs["poison_available"]:
                obs["eligible_poison_targets"] = [p.id for p in state.players if p.alive]
        else:
            obs["message"] = "Night falls. You are asleep. You cannot act now."
            obs["asleep"] = True

    # Seer phase
    elif phase == Phase.NIGHT_SEER:
        if player.role == Role.SEER and player.alive:
            eligible = [p.id for p in state.players if p.alive and p.id != player.id]
            obs["message"] = "Seer wakes up. Choose one player to investigate."
            obs["eligible_targets"] = eligible
        else:
            obs["message"] = "Night falls. You are asleep. You cannot act now."
            obs["asleep"] = True

    else:
        obs["message"] = "Night falls. You are asleep. You cannot act now."
        obs["asleep"] = True


def _add_discussion_observation(obs: dict, state: GameState, player: Player):
    if not player.alive:
        obs["message"] = "You are dead. Spectating only."
        obs["can_speak"] = False
        return

    obs["can_speak"] = True
    obs["discussion_round"] = state.discussion_round

    # Show speeches from this day so far
    day_speeches = [s for s in state.day_speeches if s.get("day") == state.day]
    obs["speeches_this_day"] = day_speeches
    obs["message"] = f"Day {state.day} Discussion — Round {state.discussion_round + 1}. Speak your mind."


def _add_voting_observation(obs: dict, state: GameState, player: Player):
    if not player.alive:
        obs["message"] = "You are dead. You cannot vote."
        obs["can_vote"] = False
        return

    obs["can_vote"] = True
    obs["eligible_targets"] = [p.id for p in state.players if p.alive]
    obs["can_skip"] = state.config.allow_skip_vote


def _add_execution_observation(obs: dict, state: GameState, player: Player):
    """After voting resolves, show result."""
    pass  # result is in public_history


def _add_hunter_shot_observation(obs: dict, state: GameState, player: Player):
    if player.role == Role.HUNTER and not player.alive:
        obs["message"] = "You died as the Hunter. You may shoot one living player or skip."
        obs["eligible_targets"] = [p.id for p in state.players if p.alive]
        obs["can_skip"] = True
    else:
        obs["message"] = "No action required."
