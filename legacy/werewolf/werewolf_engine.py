"""
Werewolf Game Engine — State Machine, Roles, Night/Day Resolution
Pure logic, no I/O. All game state is JSON-serializable.
"""
import json
import random
import os
from enum import Enum
from typing import Optional


# ── File paths ──
STATE_FILE = os.environ.get("WEREWOLF_STATE_FILE", "/opt/agentnet/werewolf_data/werewolf_state.json")
STATS_FILE = os.environ.get("WEREWOLF_STATS_FILE", "/opt/agentnet/werewolf_data/werewolf_stats.json")


class GamePhase(str, Enum):
    SETUP = "setup"
    NIGHT_WOLVES = "night_wolves"       # Wolves choose victim
    NIGHT_SEER = "night_seer"           # Seer investigates
    NIGHT_GUARD = "night_guard"         # Guard chooses protect
    NIGHT_WITCH = "night_witch"         # Witch decides
    NIGHT_RESOLVE = "night_resolve"     # Calculate night results
    DAY_ANNOUNCE = "day_announce"       # Announce night results
    DAY_DISCUSSION = "day_discussion"   # Public discussion
    DAY_VOTE = "day_vote"               # Vote to lynch
    DAY_RESOLVE = "day_resolve"         # Resolve lynching + hunter
    GAME_OVER = "game_over"


ROLES = ["Werewolf", "Werewolf", "Seer", "Guard", "Witch", "Hunter"]
# 6 players: 2 wolves, 1 seer, 1 guard, 1 witch, 1 hunter

MAX_ROUNDS = 15
SLEEP_BETWEEN_PHASES = 2  # seconds to wait between phases (for readability)


class Player:
    """A player in the game."""
    def __init__(self, name: str, role: str, player_id: str):
        self.name = name
        self.role = role
        self.player_id = player_id
        self.alive = True
        self.avatar_index = 0

    def to_dict(self):
        return {
            "name": self.name,
            "role": self.role,
            "player_id": self.player_id,
            "alive": self.alive,
            "avatar_index": self.avatar_index,
        }

    @classmethod
    def from_dict(cls, d):
        p = cls(d["name"], d["role"], d["player_id"])
        p.alive = d["alive"]
        p.avatar_index = d.get("avatar_index", 0)
        return p


class GameState:
    """Complete game state, JSON-serializable."""

    def __init__(self):
        self.game_id = ""
        self.phase = GamePhase.SETUP
        self.round = 0
        self.players: list[Player] = []
        self.alive_count = 0
        self.wolf_count = 0
        
        # Night action storage
        self.wolf_votes: dict[str, str] = {}       # wolf_id -> target_id
        self.guard_target: Optional[str] = None     # player_id
        self.seer_target: Optional[str] = None      # player_id
        self.seer_result: Optional[bool] = None     # True=wolf, False=not
        self.witch_save_used = False
        self.witch_kill_used = False
        self.witch_save_target: Optional[str] = None
        self.witch_kill_target: Optional[str] = None
        
        # Night resolution
        self.night_death: Optional[str] = None      # player_id killed
        self.night_message = ""
        self.night_attacked: Optional[str] = None   # who wolves targeted
        
        # Day voting
        self.day_votes: dict[str, str] = {}         # voter_id -> target_id
        self.lynched: Optional[str] = None
        self.hunter_target: Optional[str] = None
        
        # Public info
        self.public_thread_id: Optional[str] = None
        self.game_log: list[str] = []               # public messages
        self.revealed_roles: dict[str, str] = {}    # player_id -> role (when dead)
        
        # Game over
        self.winner = ""
        self.game_count = 0

    def to_dict(self):
        return {
            "game_id": self.game_id,
            "phase": self.phase.value,
            "round": self.round,
            "players": [p.to_dict() for p in self.players],
            "alive_count": self.alive_count,
            "wolf_count": self.wolf_count,
            "night_death": self.night_death,
            "night_message": self.night_message,
            "lynched": self.lynched,
            "hunter_target": self.hunter_target,
            "public_thread_id": self.public_thread_id,
            "game_log": self.game_log[-50:],  # last 50 messages
            "revealed_roles": self.revealed_roles,
            "winner": self.winner,
            "game_count": self.game_count,
            "round": self.round,
        }

    def get_public_state(self):
        """What spectators see — roles hidden for alive players."""
        players_public = []
        for p in self.players:
            info = {"name": p.name, "player_id": p.player_id, "alive": p.alive}
            if not p.alive and p.player_id in self.revealed_roles:
                info["role"] = self.revealed_roles[p.player_id]
            elif p.alive and p.player_id in self.revealed_roles:
                info["role"] = self.revealed_roles[p.player_id]
            else:
                info["role"] = "Unknown"
            players_public.append(info)
        return {
            "phase": self.phase.value,
            "round": self.round,
            "players": players_public,
            "alive_count": self.alive_count,
            "night_message": self.night_message,
            "lynched": self.lynched,
            "winner": self.winner,
            "game_log": self.game_log[-50:],
            "game_count": self.game_count,
        }

    def get_player_context(self, player_id: str) -> dict:
        """What a specific player knows (private info included)."""
        player = self.get_player(player_id)
        if not player:
            return {}
        
        context = {
            "your_name": player.name,
            "your_role": player.role,
            "alive_players": [p.name for p in self.players if p.alive],
            "all_players": [p.name for p in self.players],
            "round": self.round,
            "phase": self.phase.value,
            "game_log": self.game_log[-20:],
        }
        
        # Add seer info if available
        if player.role == "Seer" and self.seer_result is not None and self.seer_target:
            target_name = self.get_player(self.seer_target)
            if target_name:
                context["seer_result"] = {
                    "target": target_name.name,
                    "is_werewolf": self.seer_result,
                }
        
        # Add witch info
        if player.role == "Witch" and self.night_attacked:
            attacked = self.get_player(self.night_attacked)
            if attacked:
                context["wolf_target"] = attacked.name
            context["witch_save_used"] = self.witch_save_used
            context["witch_kill_used"] = self.witch_kill_used
        
        # Revealed roles of dead players
        context["known_deaths"] = {}
        for pid, role in self.revealed_roles.items():
            dead = self.get_player(pid)
            if dead:
                context["known_deaths"][dead.name] = role
        
        return context

    def get_player(self, player_id: str) -> Optional[Player]:
        for p in self.players:
            if p.player_id == player_id:
                return p
        return None

    def get_player_by_name(self, name: str) -> Optional[Player]:
        for p in self.players:
            if p.name == name:
                return p
        return None

    def get_alive_player_names(self) -> list[str]:
        return [p.name for p in self.players if p.alive]

    def get_wolf_names(self) -> list[str]:
        return [p.name for p in self.players if p.role == "Werewolf" and p.alive]

    def get_role_player_names(self, role: str) -> list[str]:
        return [p.name for p in self.players if p.role == role and p.alive]

    def log(self, message: str):
        self.game_log.append(f"[R{self.round}] {message}")

    def reveal_role(self, player_id: str):
        """Reveal a player's role (when they die)."""
        player = self.get_player(player_id)
        if player:
            self.revealed_roles[player_id] = player.role

    def save(self):
        """Persist state to JSON file."""
        with open(STATE_FILE, "w") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls):
        """Load state from JSON file. Returns None if no saved game."""
        if not os.path.exists(STATE_FILE):
            return None
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state = cls()
            state.game_id = data.get("game_id", "")
            state.phase = GamePhase(data.get("phase", "setup"))
            state.round = data.get("round", 0)
            state.players = [Player.from_dict(p) for p in data.get("players", [])]
            state.alive_count = data.get("alive_count", 0)
            state.wolf_count = data.get("wolf_count", 0)
            state.night_death = data.get("night_death")
            state.night_message = data.get("night_message", "")
            state.lynched = data.get("lynched")
            state.hunter_target = data.get("hunter_target")
            state.public_thread_id = data.get("public_thread_id")
            state.game_log = data.get("game_log", [])
            state.revealed_roles = data.get("revealed_roles", {})
            state.winner = data.get("winner", "")
            state.game_count = data.get("game_count", 0)
            return state
        except Exception:
            return None


class GameEngine:
    """Game logic — roles, resolution, win-check."""

    @staticmethod
    def init_game(player_names: list[str], game_count: int = 0) -> GameState:
        """
        Create a new game with random role assignment.
        player_names: list of 6 player names.
        """
        assert len(player_names) >= 6, "Need at least 6 players"
        
        state = GameState()
        state.game_id = f"werewolf-{game_count + 1}-{random.randint(1000, 9999)}"
        state.game_count = game_count
        state.round = 0
        state.phase = GamePhase.NIGHT_WOLVES
        
        # Assign roles (shuffle)
        roles = ROLES.copy()
        # If more than 6 players, add villagers
        extra = len(player_names) - 6
        for _ in range(extra):
            roles.append("Villager")
        random.shuffle(roles)
        
        # Create players
        for i, name in enumerate(player_names):
            state.players.append(Player(name, roles[i], f"player_{i}"))
        
        state.alive_count = len(state.players)
        state.wolf_count = sum(1 for p in state.players if p.role == "Werewolf")
        
        state.log(f"Game {state.game_id} started! {len(state.players)} players, {state.wolf_count} wolves.")
        
        return state

    @staticmethod
    def advance_phase(state: GameState) -> GameState:
        """Move to next phase in the game cycle."""
        phase_order = [
            GamePhase.NIGHT_WOLVES,
            GamePhase.NIGHT_SEER,
            GamePhase.NIGHT_GUARD,
            GamePhase.NIGHT_WITCH,
            GamePhase.NIGHT_RESOLVE,
            GamePhase.DAY_ANNOUNCE,
            GamePhase.DAY_DISCUSSION,
            GamePhase.DAY_VOTE,
            GamePhase.DAY_RESOLVE,
        ]
        
        current_idx = phase_order.index(state.phase) if state.phase in phase_order else -1
        
        if current_idx >= 0 and current_idx < len(phase_order) - 1:
            state.phase = phase_order[current_idx + 1]
        elif state.phase == GamePhase.DAY_RESOLVE:
            # Start new round
            state.round += 1
            if state.round > MAX_ROUNDS:
                state.phase = GamePhase.GAME_OVER
                state.winner = "Draw — max rounds reached"
                state.log("Game over: Max rounds reached! It's a draw.")
            else:
                state.phase = GamePhase.NIGHT_WOLVES
                # Reset night actions
                state.wolf_votes = {}
                state.guard_target = None
                state.seer_target = None
                state.seer_result = None
                state.witch_save_target = None
                state.witch_kill_target = None
                state.night_death = None
                state.night_message = ""
                state.night_attacked = None
                state.day_votes = {}
                state.lynched = None
                state.hunter_target = None
                state.log(f"--- Round {state.round} begins ---")
        
        return state

    @staticmethod
    def resolve_night(state: GameState) -> GameState:
        """
        Resolve night actions:
        1. Use pre-determined victim (from night_attacked)
        2. Guard protects (saves if matches wolf target)
        3. Witch can save or kill
        4. Determine actual death
        """
        victim_id = state.night_attacked
        
        if victim_id:
            victim = state.get_player(victim_id)
            vname = victim.name if victim else "Unknown"
        else:
            vname = "No one"
        
        # 2. Guard protection
        guard_saved = (state.guard_target and state.guard_target == victim_id)
        
        # 3. Witch action — check if witch chose to save this victim
        witch_saved = (state.witch_save_target == victim_id)
        
        # 4. Calculate result
        if victim_id is None:
            state.night_death = None
            state.night_message = "The wolves were indecisive. No one was attacked."
            state.log("Night passed peacefully — wolves couldn't decide.")
        elif guard_saved or state.witch_save_target == victim_id:
            state.night_death = None
            state.night_message = f"Someone was attacked but saved! {vname} survives."
            state.witch_save_used = True
            state.log(f"{vname} was attacked by wolves but saved!")
        else:
            state.night_death = victim_id
            state.night_message = f"{vname} was killed by the wolves!"
            state.log(f"{vname} was killed by wolves!")
            state.get_player(victim_id).alive = False
            state.alive_count -= 1
            state.reveal_role(victim_id)
        
        # Witch extra kill
        if state.witch_kill_target:
            target = state.get_player(state.witch_kill_target)
            if target and target.alive:
                target.alive = False
                state.alive_count -= 1
                state.reveal_role(state.witch_kill_target)
                state.night_message += f" Also, the Witch poisoned {target.name}!"
                state.log(f"Witch poisoned {target.name}!")
                state.witch_kill_used = True
        
        # Check win after night
        state.wolf_count = sum(1 for p in state.players if p.role == "Werewolf" and p.alive)
        
        return state

    @staticmethod
    def resolve_day(state: GameState) -> GameState:
        """
        Resolve day voting:
        1. Count votes
        2. Most voted gets lynched
        3. Hunter revenge if lynched
        4. Check win condition
        """
        if not state.day_votes:
            state.lynched = None
            state.night_message += " No one was voted out today."
            state.log("Day ended with no votes.")
        else:
            votes = list(state.day_votes.values())
            # Find most voted (majority), tie = no lynch
            vote_counts = {}
            for v in votes:
                vote_counts[v] = vote_counts.get(v, 0) + 1
            max_votes = max(vote_counts.values())
            top_targets = [p for p, c in vote_counts.items() if c == max_votes]
            
            if len(top_targets) == 1:
                state.lynched = top_targets[0]
                target = state.get_player(state.lynched)
                if target and target.alive:
                    target.alive = False
                    state.alive_count -= 1
                    state.reveal_role(state.lynched)
                    state.log(f"{target.name} (was {target.role}) was lynched!")
                    
                    # Hunter revenge
                    if target.role == "Hunter" and state.hunter_target:
                        hunter_kill = state.get_player(state.hunter_target)
                        if hunter_kill and hunter_kill.alive:
                            hunter_kill.alive = False
                            state.alive_count -= 1
                            state.reveal_role(state.hunter_target)
                            state.log(f"Hunter {target.name} took {hunter_kill.name} down!")
            else:
                state.lynched = None
                state.log("Vote tied — no one was lynched today.")
        
        # Check win condition
        state.wolf_count = sum(1 for p in state.players if p.role == "Werewolf" and p.alive)
        village_count = state.alive_count - state.wolf_count
        
        if state.wolf_count <= 0:
            state.winner = "Village"
            state.phase = GamePhase.GAME_OVER
            state.log("🎉 Village wins! All werewolves eliminated!")
        elif state.wolf_count >= village_count:
            state.winner = "Werewolves"
            state.phase = GamePhase.GAME_OVER
            state.log("🐺 Werewolves win! They've taken over the village!")
        
        return state


# ── Stats tracking ──

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "games_played": 0,
        "village_wins": 0,
        "wolf_wins": 0,
        "player_stats": {},
        "game_history": [],
    }

def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

def update_stats(state: GameState):
    stats = load_stats()
    stats["games_played"] += 1
    if state.winner == "Village":
        stats["village_wins"] += 1
    elif state.winner == "Werewolves":
        stats["wolf_wins"] += 1
    
    for p in state.players:
        pid = p.player_id
        if pid not in stats["player_stats"]:
            stats["player_stats"][pid] = {
                "name": p.name,
                "games": 0,
                "wins": 0,
                "kills": 0,
                "deaths": 0,
                "roles": {},
            }
        ps = stats["player_stats"][pid]
        ps["games"] += 1
        ps["roles"][p.role] = ps["roles"].get(p.role, 0) + 1
        
        # Win count
        if state.winner == "Village" and p.role != "Werewolf":
            ps["wins"] += 1
        elif state.winner == "Werewolves" and p.role == "Werewolf":
            ps["wins"] += 1
        
        if not p.alive:
            ps["deaths"] += 1
    
    save_stats(stats)
    return stats
