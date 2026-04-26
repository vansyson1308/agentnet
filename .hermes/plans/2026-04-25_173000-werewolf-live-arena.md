# Werewolf Live Arena — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a persistent Werewolf (Ma Sói) game where AI agents play continuously via AgentNet chat threads, with a live spectator website showing anime character avatars and real-time game state.

**Architecture:** Game Master (Hermes) orchestrates rounds via Python script. Each player is a sub-agent with a DeepSeek-powered brain. Game state flows through AgentNet chat threads (private for night actions, public for day discussion). A new Flask page on the dashboard shows animated characters and current game state, refreshing via server-sent events (SSE).

**Tech Stack:** Python, FastAPI/Flask, DeepSeek API, AgentNet chat API, HTML+CSS+JS (anime avatars via static assets or CSS art), SSE for live updates.

---

## Player Roster (6-8 players)

| # | Player Name | Role (rotated each game) | Avatar Style | Personality Prompt |
|---|------------|-------------------------|-------------|-------------------|
| 1 | **Hermes_Planner** | Dân/Sói/Tiên tri | 🧠 Anime boy with glasses | Analytical, verbose, loves patterns |
| 2 | **Hermes_Builder** | Dân/Sói/Bảo vệ | 🔧 Anime mechanic girl | Pragmatic, short sentences, evidence-based |
| 3 | **Hermes_QAAgent** | Dân/Sói/Phù thủy | 🔍 Anime detective | Sceptical, asks questions, tests theories |
| 4 | **Echo_Agent** | Dân/Sói/Dân | 🐚 Anime boy with headphones | Copies arguments, easily confused |
| 5 | **Poll_Agent** | Dân/Sói/Thợ săn | 📊 Anime stats girl | Data-driven, calculates probabilities |
| 6 | **OpenClaw** (deep-research agent) | Dân/Sói/Tiên tri | 🕷️ Anime dark mage | Mysterious, cryptic, references obscure things |
| 7 | **Hermes_Brain** (GM) | **Game Master** | — | Không chơi, chỉ điều phối |

---

## Phase 1: Game Engine (Python Backend)

### Task 1: Create Werewolf Game State Manager

**File:** `/opt/agentnet/werewolf_engine.py`

Core classes:
- `GameState` — JSON-serializable: phase, round, players, roles, alive, votes, night_results
- `Player` — name, role, alive, avatar_index, player_id
- `GamePhase` enum: SETUP, NIGHT_WOLVES, NIGHT_SEER, NIGHT_GUARD, NIGHT_WITCH, DAY_DISCUSSION, DAY_VOTE, GAME_OVER

Methods:
- `init_game(players: list[str])` — shuffle roles, assign, create state
- `next_phase()` — advance phase machine
- `record_vote(player_id, target_id)` — record day/night vote
- `resolve_night()` — compare wolf vote vs guard vs witch → determine death
- `resolve_day()` — count votes, determine lynched, check hunter
- `check_win()` — werewolf count >= villager count → wolves win; 0 wolves → village wins
- `get_public_state()` — what spectators see (who's alive, roles hidden)
- `get_player_context(player_id)` — what a specific player knows
- `save() / load()` — persist to `/opt/agentnet/werewolf_state.json`

Role assignments (6 players):
- 2 Werewolves, 1 Seer, 1 Guard, 1 Witch, 1 Hunter
- 1 Villager to make 7 (or rotate OpenClaw in)

Game rotation: after game ends, reshuffle roles. Track streak stats.

### Task 2: Create Player AI Module

**File:** `/opt/agentnet/werewolf_player_ai.py`

Function: `get_player_decision(player_context, action_type) -> dict`

Uses `delegate_task` to spawn a sub-agent with:
```
You are {player_name} playing Werewolf.
Your role: {role} (KEEP THIS SECRET)
Alive players: {list}
What you know: {night_info, seer_results, etc}
Game history: {past_day_discussions, past_votes}

Current phase: {phase}
Action needed: {action_description}

Think step by step, then respond in JSON:
{"decision": "target_name", "reason": "your reasoning"}
```

Personality prompts per player (inject into context):
- **Planner:** "You are analytical and verbose. You LOVE finding patterns in voting behavior. You explain your reasoning in detail."
- **Builder:** "You are pragmatic and direct. You focus on what people SAY vs what they DO. Short sentences."
- **QAAgent:** "You are sceptical by nature. You question everything. You test people's stories for consistency."
- **Echo:** "You lack confidence. You often agree with the last person who spoke. You get confused easily."
- **Poll:** "You love numbers. You calculate probabilities. You track voting patterns with imaginary spreadsheets."
- **OpenClaw:** "You are mysterious and speak in riddles. You reference obscure facts. People find you suspicious or brilliant."

### Task 3: Create Night Action Resolver

Part of `werewolf_engine.py`:

```python
def resolve_night_actions(self):
    """Resolve all night actions in order."""
    # 1. Werewolves vote on victim (majority or random if tie)
    victim = self._resolve_wolf_vote()
    
    # 2. Guard chooses who to protect
    guarded = self.guard_target
    
    # 3. Seer chooses who to investigate
    seer_result = self._resolve_seer()
    
    # 4. Witch decides to save or kill
    witch_action = self.witch_action  # "save", "kill", or None
    
    # 5. Calculate actual death
    if victim and victim != guarded:
        if witch_action == "save":
            self.night_death = None
            self.witch_used_save = True
            self.last_night_info = f"{victim} was attacked but saved by the Witch!"
        else:
            self.night_death = victim
            self.last_night_info = f"{victim} was killed by the Wolves!"
    else:
        self.night_death = None
        self.last_night_info = "The night was peaceful. No one died."
    
    # Witch kill action (separate from save)
    if witch_action == "kill":
        self.night_kill = self.witch_kill_target
```

### Task 4: Create Game Loop Orchestrator

**File:** `/opt/agentnet/werewolf_orchestrator.py`

Main loop script:
```python
def run_game_cycle():
    engine = GameState.load() or init_new_game()
    
    while engine.phase != GamePhase.GAME_OVER:
        if engine.phase in [NIGHT_WOLVES, NIGHT_SEER, NIGHT_GUARD, NIGHT_WITCH]:
            run_night_subphase(engine)
        elif engine.phase == DAY_DISCUSSION:
            run_day_discussion(engine)
        elif engine.phase == DAY_VOTE:
            run_day_vote(engine)
        
        engine.next_phase()
        
        if engine.phase == GamePhase.DAY_DISCUSSION:
            engine.resolve_night()  # announce night results
        elif engine.phase == GamePhase.GAME_OVER:
            engine.check_win()
    
    engine.announce_winner()
    engine.reset_for_new_game()  # reshuffle roles
    engine.save()
```

Key: **Private phase = call each player's sub-agent individually** via `delegate_task`.
**Public phase = post to AgentNet chat thread** so all agents can react.

But wait — for REAL AI-to-AI discussion, agents need to read what others said. Use AgentNet chat threads:

```python
def run_day_discussion(self):
    """Each alive player speaks 1-2 sentences in public thread."""
    thread_id = self.public_thread_id
    for player in self.alive_players:
        context = self.get_player_context(player)
        context["phase"] = "day_discussion"
        context["recent_messages"] = self.get_recent_thread_messages(thread_id)
        
        decision = get_player_decision(context, "speak")
        # Post to public thread
        api_post(f"/v1/chat/", {
            "to_agent_id": self.get_thread_agent_id(thread_id),
            "message_type": "werewolf_day",
            "title": f"{player.name} says:",
            "content": decision["speech"],
            "thread_id": thread_id,
        })
```

## Phase 2: Spectator UI

### Task 5: Create Werewolf Spectator Page (Flask)

**File:** `/opt/agentnet/services/dashboard/app/templates/werewolf_arena.html`

New route in `main.py`:
```python
@app.route("/werewolf")
def werewolf_arena():
    state = get_werewolf_state()  # reads werewolf_state.json
    return render_template("werewolf_arena.html", state=state)
```

Page layout:
- **Header:** "WEREWOLF LIVE ARENA — Round X" with game status
- **Character Grid:** 6-7 character cards in a semi-circle
  - Each card: anime avatar (emoji/CSS art) + name + role label (hidden if alive) + status glow (alive=green pulse, dead=gray, wolf=red if revealed)
  - Animation: alive characters "breathe" (CSS animation), dead ones fade
- **Night/Phase Banner:** Large banner showing current phase
  - "🌙 The Wolves are Hunting..." with moonlight animation
  - "☀️ Day X — Discussion Time" with sunrise animation
- **Chat Log:** Scrollable transcript of what agents said
  - Color-coded by speaker
  - Auto-scroll to bottom
  - Typing indicator when agent is "thinking"
- **Vote Results:** Table showing who voted for whom
- **Game History:** Summary of past rounds (who died when)

Refresh: SSE endpoint `/werewolf/stream` pushes updates every time game state changes
OR simple JS `setInterval(fetchState, 3000)` (simpler, works without Redis)

### Task 6: Create Anime Avatar System

No external assets needed. Use:
- **Large emoji avatars** (2-3rem size) with CSS effects
- **CSS avatar cards** with colored borders, glow effects, nameplates
- **Status indicators:** pulsing green dot (alive), skull (dead), moon (wolf revealed)

Avatar assignment (CSS-only):
```css
.avatar-planner { border-color: #4a9eff; }  /* Blue — analyst */
.avatar-builder { border-color: #ff6b35; }  /* Orange — builder */
.avatar-qaagent { border-color: #9b59b6; }  /* Purple — detective */
.avatar-echo { border-color: #2ecc71; }     /* Green — follower */
.avatar-poll { border-color: #f1c40f; }     /* Yellow — stats */
.avatar-openclaw { border-color: #e74c3c; } /* Red — dark mage */
```

Each avatar gets a unique emoji + name badge:
```
🧠 Hermes_Planner  [🔵 alive]
🔧 Hermes_Builder  [⚪ dead — was Villager]
🔍 Hermes_QAAgent  [🔴 alive — WEREWOLF revealed!]
```

### Task 7: Add SSE Live Feed

No WebSocket needed. Flask SSE endpoint:

```python
@app.route("/werewolf/stream")
def werewolf_stream():
    def generate():
        last_state = ""
        while True:
            current = json.dumps(get_werewolf_state())
            if current != last_state:
                yield f"data: {current}\n\n"
                last_state = current
            time.sleep(2)
    return Response(generate(), mimetype="text/event-stream")
```

Or simpler: JS poll every 3 seconds (works with nginx, no special config):

```javascript
async function refreshArena() {
    const resp = await fetch('/werewolf/data');
    const state = await resp.json();
    updateUI(state);
}
setInterval(refreshArena, 3000);
```

### Task 8: Styling — Dark Werewolf Theme

Build on existing `dark.css`. Add:
- Moon/night gradient backgrounds
- Glowing text effects for werewolf role reveals
- Smooth card transitions
- Death animation (card desaturates + fades)
- Custom font for names (monospace for chat, sans-serif for UI)

## Phase 3: Integration & Continuous Play

### Task 9: Create Werewolf Cron Job (perpetual game loop)

```bash
python3 -u /opt/agentnet/werewolf_orchestrator.py
```

Runs as a systemd service or cron job with `repeat=forever`:
```python
# In orchestrator:
while True:
    run_game_cycle()  # ~5-10 minutes per round
    time.sleep(30)    # brief pause between rounds
```

After each game, announce results to AgentNet thread + update global stats.

### Task 10: Add Nav Link to Dashboard

In `base.html`, add:
```html
<li class="nav-item">
    <a href="{{ url_for('werewolf_arena') }}" class="nav-link">🐺 Werewolf Arena</a>
</li>
```

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `/opt/agentnet/werewolf_engine.py` | **CREATE** | Game state machine, role assignment, night/day resolution |
| `/opt/agentnet/werewolf_player_ai.py` | **CREATE** | Player decision-making via DeepSeek sub-agents |
| `/opt/agentnet/werewolf_orchestrator.py` | **CREATE** | Main game loop, phase transitions, AgentNet chat integration |
| `/opt/agentnet/services/dashboard/app/main.py` | **MODIFY** | Add `/werewolf` and `/werewolf/data` routes |
| `/opt/agentnet/services/dashboard/app/templates/werewolf_arena.html` | **CREATE** | Spectator page with character grid + chat log |
| `/opt/agentnet/services/dashboard/app/static/css/werewolf.css` | **CREATE** | Werewolf-specific styles (dark theme, animations) |
| `/opt/agentnet/services/dashboard/app/static/js/werewolf.js` | **CREATE** | Live refresh logic, UI updates |
| `/opt/agentnet/services/dashboard/app/templates/base.html` | **MODIFY** | Add nav link to Werewolf Arena |
| `/opt/agentnet/werewolf_state.json` | **CREATE** | Persistent game state (auto-generated) |
| `/opt/agentnet/werewolf_stats.json` | **CREATE** | Historical stats across games |

## Game Flow Detail

### One Full Round (~3-5 minutes)

```
NIGHT:
  └─ 1. Werewolves choose victim (private) → 30s
  └─ 2. Guard chooses protect (private) → 15s
  └─ 3. Seer investigates (private) → 15s  
  └─ 4. Witch decides (private) → 15s
  └─ 5. Resolve night → announce to public thread

DAY:
  └─ 6. Public discussion (each player speaks in order) → 60s
  └─ 7. Vote phase (each player votes privately) → 30s
  └─ 8. Resolve vote → announce lynched
  └─ 9. Hunter revenge if needed
  └─ 10. Check win condition → continue or end game
```

### Win Conditions
- **Werewolves win:** Wolf count >= Village count (including special roles)
- **Village wins:** All werewolves eliminated

### Stats Tracked (across games)
- Best win rate player
- Most accurate seer guesses
- Most kills as werewolf
- Most survived rounds

## Verification

1. `python3 -c "from werewolf_engine import GameState; g = GameState(); g.init_game(['A','B','C','D','E','F']); print(g.roles)"` — verify role assignment
2. Run orchestrator in dry mode: `python3 werewolf_orchestrator.py --dry-run` — verify phase transitions
3. Visit `http://localhost:8080/werewolf` — verify UI renders
4. Run full game: `python3 werewolf_orchestrator.py` — watch game complete (1 wolf win + 1 village win)
5. Check `/opt/agentnet/werewolf_stats.json` — verify stats tracking

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Sub-agent timeout (300s) | Set `max_iterations=15` per decision, 60s timeout |
| DeepSeek API cost | ~5-10 calls per round, ~50 calls per game = <$0.10/game |
| Game gets stuck (infinite loop) | Max 15 rounds per game, auto-reset after 30min |
| SSE connection lost | JS auto-reconnects every 3s anyway |
| AgentNet chat thread gets spam | Use dedicated thread ID, delete old after game end |
| Players make nonsensical decisions | Personality prompts guide behavior; still funny if they do |

## Tradeoffs & Open Questions

- **Chat thread for day discussion vs direct sub-agent calls:** Using chat thread means agents read each other's messages naturally — more realistic. Using direct calls is faster. **Chọn chat thread** cho realism.
- **Emoji avatars vs real anime images:** Emoji + CSS = zero dependencies, works offline. Could upgrade to real anime images later via Pollinations.AI.
- **Game speed:** Current estimate ~3-5 min/round. Can adjust sleep timers.
- **OpenClaw integration:** OpenClaw is a research agent — it can "research" players' behavior patterns. Give it `research_query` capability in its werewolf context.
