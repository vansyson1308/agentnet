"""
Werewolf Game Orchestrator — Main game loop integrating Engine + Player AI + AgentNet chat.
Runs continuously as a service, managing game cycles.
"""
import json
import os
import sys
import time
import random
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werewolf_engine import (
    GameEngine, GameState, GamePhase, 
    STATE_FILE, STATS_FILE, 
    load_stats, save_stats, update_stats,
    SLEEP_BETWEEN_PHASES,
)

# ── Configuration ──
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")
AGENTNET_USER_EMAIL = os.environ.get("AGENTNET_USER_EMAIL", "sonnv.hd34@gmail.com")
AGENTNET_USER_PASSWORD = os.environ.get("AGENTNET_USER_PASSWORD", "TestPass123")

# Player roster (6 players)
PLAYER_NAMES = ["Planner", "Builder", "QAAgent", "Echo", "Poll", "OpenClaw"]

# Game timing (seconds)
NIGHT_TIMEOUT = 30       # max wait for night actions
DAY_DISCUSSION_TIMEOUT = 45
VOTE_TIMEOUT = 25
PAUSE_BETWEEN_GAMES = 10  # pause before next game


class WerewolfOrchestrator:
    """Main orchestrator — manages game loop, AgentNet integration, state persistence."""

    def __init__(self):
        self.state: GameState = None
        self.game_count = 0
        self._token = None
        self._token_expiry = 0
        self.running = True

    # ── Auth helpers ──
    def _ensure_token(self) -> str:
        """Get auth token for AgentNet API."""
        import urllib.request
        
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        
        try:
            data = f"username={AGENTNET_USER_EMAIL}&password={AGENTNET_USER_PASSWORD}"
            req = urllib.request.Request(
                f"{REGISTRY_URL}/v1/auth/user/login",
                data.encode(),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                self._token = result.get("access_token", "")
                self._token_expiry = now + 1500
        except Exception as e:
            print(f"[Auth] Login failed: {e}")
            self._token = ""
        
        return self._token

    def _api_post(self, path: str, data: dict) -> dict:
        """POST to AgentNet API."""
        import urllib.request
        
        token = self._ensure_token()
        if not token:
            return {}
        
        url = f"{REGISTRY_URL}{path}"
        body = json.dumps(data).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"[API] POST {path}: {e}")
            return {}

    def _api_get(self, path: str) -> dict:
        """GET from AgentNet API."""
        import urllib.request
        
        token = self._ensure_token()
        if not token:
            return {}
        
        url = f"{REGISTRY_URL}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print(f"[API] GET {path}: {e}")
            return {}

    # ── Game management ──
    def start_new_game(self):
        """Initialize a new game with shuffled roles."""
        self.game_count += 1
        self.state = GameEngine.init_game(PLAYER_NAMES, self.game_count)
        self.state.public_thread_id = f"werewolf-{self.game_count}"
        print(f"\n{'='*60}")
        print(f"🐺 WEREWOLF GAME #{self.game_count} STARTED!")
        print(f"Roles: {[(p.name, p.role) for p in self.state.players]}")
        print(f"{'='*60}")
        
        # Log initial state
        self._broadcast_state()
        self.state.save()
        return self.state

    def _broadcast_state(self):
        """Save state so the dashboard can read it."""
        self.state.save()
        # Also notify via AgentNet if we had a thread

    # ── Night phase ──
    def run_night(self):
        """Run the full night cycle (4 sub-phases)."""
        state = self.state
        print(f"\n🌙 ROUND {state.round + 1} — NIGHT 🌙")
        state.log(f"🌙 Night {state.round + 1}")
        state.phase = GamePhase.NIGHT_WOLVES
        
        # 1. Werewolves choose victim
        self._night_wolves()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # 2. Seer investigates
        state.phase = GamePhase.NIGHT_SEER
        self._night_seer()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # 3. Guard protects
        state.phase = GamePhase.NIGHT_GUARD
        self._night_guard()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # 4. Witch decides
        state.phase = GamePhase.NIGHT_WITCH
        self._night_witch()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # 5. Resolve
        state.phase = GamePhase.NIGHT_RESOLVE
        GameEngine.resolve_night(state)
        
        # Check if game over
        if state.winner:
            state.phase = GamePhase.GAME_OVER
            return
        
        state.phase = GamePhase.DAY_ANNOUNCE
        print(f"[Night result] {state.night_message}")
        state.log(state.night_message)
        self._broadcast_state()

    def _night_wolves(self):
        """Werewolves vote on victim."""
        state = self.state
        wolves = [p for p in state.players if p.role == "Werewolf" and p.alive]
        
        if len(wolves) <= 1:
            # Solo wolf or none — just choose
            wolf_names = [w.name for w in wolves]
            non_wolves = [p for p in state.players if p.role != "Werewolf" and p.alive]
            if wolves and non_wolves:
                context = state.get_player_context(wolves[0].player_id)
                decision = self._get_ai_decision(wolves[0].name, context, "night_wolves")
                target_name = decision.get("decision", non_wolves[0].name)
                target = state.get_player_by_name(target_name)
                if target and target.alive and target.role != "Werewolf":
                    state.wolf_votes[wolves[0].player_id] = target.player_id
                    print(f"🐺 {wolves[0].name} chooses to kill {target_name}")
        else:
            # Multiple wolves — each votes
            for wolf in wolves:
                context = state.get_player_context(wolf.player_id)
                alive_targets = [p.name for p in state.players if p.role != "Werewolf" and p.alive]
                context["alive_players"] = alive_targets
                decision = self._get_ai_decision(wolf.name, context, "night_wolves")
                target_name = decision.get("decision", random.choice(alive_targets) if alive_targets else "")
                target = state.get_player_by_name(target_name)
                if target and target.alive and target.role != "Werewolf":
                    state.wolf_votes[wolf.player_id] = target.player_id
                    print(f"🐺 {wolf.name} votes to kill {target_name}")
            
            # If no votes, pick random
            if not state.wolf_votes:
                non_wolves = [p for p in state.players if p.role != "Werewolf" and p.alive]
                if non_wolves:
                    target = random.choice(non_wolves)
                    for wolf in wolves:
                        state.wolf_votes[wolf.player_id] = target.player_id
                    print(f"🐺 Wolves randomly choose {target.name}")
        
        # Determine victim immediately so witch can know
        self._determine_wolf_victim(state)

    def _determine_wolf_victim(self, state):
        """Calculate wolf victim right after votes (before resolve)."""
        if state.wolf_votes:
            votes = list(state.wolf_votes.values())
            victim_id = max(set(votes), key=votes.count)
        else:
            non_wolves = [p for p in state.players if p.role != "Werewolf" and p.alive]
            victim_id = random.choice(non_wolves).player_id if non_wolves else None
        
        state.night_attacked = victim_id

    def _night_seer(self):
        """Seer investigates a player."""
        state = self.state
        seers = [p for p in state.players if p.role == "Seer" and p.alive]
        if not seers:
            return
        
        seer = seers[0]
        targets = [p for p in state.players if p.alive and p.player_id != seer.player_id]
        if not targets:
            return
        
        context = state.get_player_context(seer.player_id)
        decision = self._get_ai_decision(seer.name, context, "night_seer")
        target_name = decision.get("decision", random.choice([t.name for t in targets]))
        target = state.get_player_by_name(target_name)
        
        if target and target.alive:
            state.seer_target = target.player_id
            state.seer_result = (target.role == "Werewolf")
            result_str = "WEREWOLF!" if state.seer_result else "NOT a Wolf"
            print(f"👁️ {seer.name} investigates {target_name}: {result_str}")
            state.log(f"Seer investigated {target_name}")

    def _night_guard(self):
        """Guard protects a player."""
        state = self.state
        guards = [p for p in state.players if p.role == "Guard" and p.alive]
        if not guards:
            return
        
        guard = guards[0]
        targets = [p for p in state.players if p.alive and p.player_id != guard.player_id]
        if not targets:
            return
        
        context = state.get_player_context(guard.player_id)
        decision = self._get_ai_decision(guard.name, context, "night_guard")
        target_name = decision.get("decision", random.choice([t.name for t in targets]))
        target = state.get_player_by_name(target_name)
        
        if target and target.alive:
            state.guard_target = target.player_id
            print(f"🛡️ {guard.name} protects {target_name}")

    def _night_witch(self):
        """Witch decides to save and/or kill."""
        state = self.state
        witches = [p for p in state.players if p.role == "Witch" and p.alive]
        if not witches:
            return
        
        witch = witches[0]
        context = state.get_player_context(witch.player_id)
        
        # Witch save decision
        if not state.witch_save_used and state.night_attacked:
            attacked = state.get_player(state.night_attacked)
            if attacked and attacked.alive:
                context["wolf_target"] = attacked.name
                decision = self._get_ai_decision(witch.name, context, "night_witch_save")
                if decision.get("decision") == "save":
                    state.witch_save_target = state.night_attacked
                    print(f"🧪 {witch.name} SAVES {attacked.name}!")
                else:
                    print(f"🧪 {witch.name} lets {attacked.name} die.")
        
        # Witch kill decision
        if not state.witch_kill_used:
            targets = [p.name for p in state.players if p.alive and p.player_id != witch.player_id]
            decision = self._get_ai_decision(witch.name, context, "night_witch_kill")
            target_name = decision.get("decision", "no_kill")
            if target_name and target_name != "no_kill":
                target = state.get_player_by_name(target_name)
                if target and target.alive:
                    state.witch_kill_target = target.player_id
                    print(f"🧪 {witch.name} POISONS {target_name}!")

    # ── Day phase ──
    def run_day(self):
        """Run the full day cycle (discussion + vote)."""
        state = self.state
        print(f"\n☀️ ROUND {state.round + 1} — DAY {state.round + 1} ☀️")
        
        # Announce night results (we're in DAY_ANNOUNCE phase now)
        if state.night_message:
            print(f"[📢] {state.night_message}")
        
        # Check if game over after night
        if state.winner:
            state.phase = GamePhase.GAME_OVER
            return
        
        # Day discussion — each player speaks
        state.phase = GamePhase.DAY_DISCUSSION
        self._day_discussion()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # Vote phase
        state.phase = GamePhase.DAY_VOTE
        self._day_vote()
        time.sleep(SLEEP_BETWEEN_PHASES)
        
        # Resolve
        state.phase = GamePhase.DAY_RESOLVE
        GameEngine.resolve_day(state)
        
        # Log results
        if state.lynched:
            lynched = state.get_player(state.lynched)
            if lynched:
                print(f"⚖️ {lynched.name} (was {lynched.role}) was lynched!")
        
        # Hunter revenge
        self._hunter_revenge()
        
        self._broadcast_state()

    def _day_discussion(self):
        """Each alive player speaks in the public thread."""
        state = self.state
        alive = [p for p in state.players if p.alive]
        
        for player in alive:
            context = state.get_player_context(player.player_id)
            decision = self._get_ai_decision(player.name, context, "day_discussion")
            speech = decision.get("decision", "I'm watching everyone carefully.")
            reason = decision.get("reason", "")
            
            import werewolf_player_ai as wpa
            emoji = wpa.PERSONALITIES.get(player.name, {}).get("emoji", "🗣️")
            print(f"  {emoji} {player.name}: \"{speech[:120]}\"")
            state.log(f"{player.name}: {speech[:150]}")
            time.sleep(1)  # Small delay between speeches

    def _day_vote(self):
        """Each alive player votes."""
        state = self.state
        alive = [p for p in state.players if p.alive]
        
        for player in alive:
            others = [p.name for p in state.players if p.alive and p.player_id != player.player_id]
            if not others:
                continue
            
            context = state.get_player_context(player.player_id)
            decision = self._get_ai_decision(player.name, context, "day_vote")
            target_name = decision.get("decision", random.choice(others))
            target = state.get_player_by_name(target_name)
            
            if target and target.alive:
                state.day_votes[player.player_id] = target.player_id
                reason = decision.get("reason", "Just a feeling.")
                print(f"  ✋ {player.name} votes for {target_name}: {reason[:80]}")
                time.sleep(1)
        
        # If no votes, assign random
        if not state.day_votes:
            alive_players = [p for p in state.players if p.alive]
            if len(alive_players) >= 2:
                voter = random.choice(alive_players)
                target = random.choice([p for p in alive_players if p.player_id != voter.player_id])
                state.day_votes[voter.player_id] = target.player_id

    def _hunter_revenge(self):
        """Hunter gets revenge if killed."""
        state = self.state
        if not state.lynched:
            return
        
        lynched = state.get_player(state.lynched)
        if not lynched or lynched.role != "Hunter":
            return
        
        alive = [p for p in state.players if p.alive and p.player_id != state.lynched]
        if not alive:
            return
        
        context = state.get_player_context(state.lynched)
        decision = self._get_ai_decision(lynched.name, context, "hunter_revenge")
        target_name = decision.get("decision", "none")
        
        if target_name and target_name != "none":
            target = state.get_player_by_name(target_name)
            if target and target.alive:
                state.hunter_target = target.player_id
                target.alive = False
                state.alive_count -= 1
                state.reveal_role(target.player_id)
                print(f"🏹 {lynched.name} (Hunter) takes {target.name} down!")
                state.log(f"Hunter {lynched.name} killed {target.name}")

    # ── AI Decision ──
    def _get_ai_decision(self, player_name: str, context: dict, action_type: str) -> dict:
        """Get AI decision for a player. Uses DeepSeek if available, falls back to random."""
        import werewolf_player_ai as wpa
        try:
            return wpa.get_player_decision_sync(player_name, context, action_type)
        except Exception as e:
            print(f"[AI] Decision error for {player_name}: {e}")
            return wpa._fallback_decision(player_name, context, action_type)

    # ── End game ──
    def end_game(self):
        """End current game, announce results, reset for next."""
        state = self.state
        if not state:
            return
        
        print(f"\n{'='*60}")
        print(f"🏁 GAME OVER — Winner: {state.winner} 🏁")
        print(f"{'='*60}")
        
        # Reveal all roles
        for p in state.players:
            print(f"  {p.name}: {p.role} {'(alive)' if p.alive else '(dead)'}")
        
        state.log(f"🏁 Game over! {state.winner} wins!")
        
        # Update stats
        stats = update_stats(state)
        print(f"\n📊 Stats: {stats['games_played']} games played")
        print(f"  Village wins: {stats['village_wins']}")
        print(f"  Wolf wins: {stats['wolf_wins']}")
        
        # Save final state
        state.save()

    # ── Main loop ──
    def run_forever(self):
        """Main game loop — runs games continuously."""
        print("🐺 WEREWOLF LIVE ARENA STARTING...")
        print(f"Players: {', '.join(PLAYER_NAMES)}")
        
        while self.running:
            try:
                self.start_new_game()
                
                # Game loop
                while self.state.phase != GamePhase.GAME_OVER and self.state.round < 15:
                    # Night
                    self.run_night()
                    if self.state.winner:
                        break
                    
                    # Day
                    self.run_day()
                    
                    # If day didn't end game, advance to next round
                    if self.state.phase == GamePhase.DAY_RESOLVE:
                        self.state.round += 1
                        GameEngine.advance_phase(self.state)
                    elif self.state.phase == GamePhase.GAME_OVER:
                        pass
                    
                    self._broadcast_state()
                    time.sleep(SLEEP_BETWEEN_PHASES)
                
                # End game
                self.end_game()
                
                # Pause before next game
                print(f"\n⏳ Next game in {PAUSE_BETWEEN_GAMES}s...")
                time.sleep(PAUSE_BETWEEN_GAMES)
                
            except KeyboardInterrupt:
                print("\n👋 Shutting down Werewolf Arena.")
                self.running = False
                break
            except Exception as e:
                print(f"\n❌ Error in game loop: {e}")
                traceback.print_exc()
                print("🔄 Restarting game in 10s...")
                time.sleep(10)


# ── Entry point ──
def main():
    orchestrator = WerewolfOrchestrator()
    orchestrator.run_forever()


if __name__ == "__main__":
    main()
