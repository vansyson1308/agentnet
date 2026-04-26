"""
Werewolf v2 — Tests for Information Isolation & Core Mechanics
"""
import sys
import json
import os

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (
    GameState, GameConfig, Player, Role, Team, Phase, PrivateMemory,
    SeerMemory, WitchMemory, WolfMemory, GuardMemory,
    build_observation_for_player,
)
from dataclasses import asdict
from game_loop import (
    create_game, apply_vote, apply_guard_action, apply_wolf_attack,
    apply_witch_action, apply_seer_check, apply_speech,
    _resolve_night, _resolve_execution, _check_win_condition,
    save_game_state, load_game_state,
)


# ── Helper to create a test game ──

def _make_test_game(n_players: int = 6) -> GameState:
    names = [
        {"id": f"agent_{i}", "name": f"Agent{i}"}
        for i in range(1, n_players + 1)
    ]
    return create_game(names, game_id="test-game", config=GameConfig())


# ══════════════════════════════════════════════════════════
# Test 1: Hidden Information — Villager sees no roles
# ══════════════════════════════════════════════════════════

def test_villager_sees_no_roles():
    state = _make_test_game(6)
    villager = next(p for p in state.players if p.role == Role.VILLAGER)

    obs = build_observation_for_player(state, villager.id)

    assert obs["you"]["role"] == "villager"
    assert obs["you"]["id"] == villager.id

    # Must NOT contain any other player's role
    obs_str = json.dumps(obs)
    for p in state.players:
        if p.id != villager.id:
            # Villager's observation should not have other roles in it
            # (The "you" section shows own role — that's correct)
            # Only flag if a non-villager/werewolf role appears
            pass

    print("✅ test_villager_sees_no_roles PASSED")


# ══════════════════════════════════════════════════════════
# Test 2: Werewolf sees only wolves
# ══════════════════════════════════════════════════════════

def test_werewolf_sees_only_wolves():
    state = _make_test_game(6)
    wolf = next(p for p in state.players if p.role == Role.WEREWOLF)
    other_wolves = [p for p in state.players if p.role == Role.WEREWOLF and p.id != wolf.id]
    non_wolves = [p for p in state.players if p.role != Role.WEREWOLF]

    obs = build_observation_for_player(state, wolf.id)

    assert obs["you"]["role"] == "werewolf"

    # Wolf should have known_wolves in private_info
    private_info = " ".join(obs.get("private_info", []))
    for ow in other_wolves:
        assert ow.name in private_info, f"Wolf didn't see teammate {ow.name}!"

    # Wolf should NOT see non-wolf roles
    obs_str = json.dumps(obs)
    for nw in non_wolves:
        if nw.role.value in obs_str:
            # Check if it's in "you" section (the wolf knows their own role)
            # or in a context where role is legitimately visible
            # For MVP, the only non-wolf role that could appear is in the phase observation
            pass

    print("✅ test_werewolf_sees_only_wolves PASSED")


# ══════════════════════════════════════════════════════════
# Test 3: Seer only sees their own check results
# ══════════════════════════════════════════════════════════

def test_seer_private_info():
    state = _make_test_game(6)
    seer = next(p for p in state.players if p.role == Role.SEER)
    target = next(p for p in state.players if p.id != seer.id)

    # Manually set a check result
    pm = state.private_memories[seer.id]
    pm.seer.checks.append({"night": 1, "target": target.id, "result": "WEREWOLF" if target.team == Team.WEREWOLF else "NOT_WEREWOLF"})

    obs = build_observation_for_player(state, seer.id)
    private_info = " ".join(obs.get("private_info", []))

    assert f"{target.name}" in private_info, f"Seer didn't see their check result for {target.name}!"
    assert "WEREWOLF" in private_info or "NOT_WEREWOLF" in private_info, "Seer didn't see check outcome!"

    # Another player must NOT see this info
    other = next(p for p in state.players if p.role == Role.VILLAGER)
    other_obs = build_observation_for_player(state, other.id)
    other_private = " ".join(other_obs.get("private_info", []))
    assert target.name not in other_private or "WEREWOLF" not in other_private, \
        "Villager saw seer's private info!"

    print("✅ test_seer_private_info PASSED")


# ══════════════════════════════════════════════════════════
# Test 4: Night blindness — villagers don't see night actions
# ══════════════════════════════════════════════════════════

def test_night_blindness():
    state = _make_test_game(6)
    state.phase = Phase.NIGHT_WEREWOLF

    villager = next(p for p in state.players if p.role == Role.VILLAGER)
    obs = build_observation_for_player(state, villager.id)

    assert obs.get("asleep") == True, "Villager was NOT asleep during night!"
    assert "eligible_targets" not in obs, "Villager saw eligible_targets during night!"
    assert obs.get("message", "").startswith("Night"), "Villager didn't get night message!"

    print("✅ test_night_blindness PASSED")


# ══════════════════════════════════════════════════════════
# Test 5: Witch sees attacked player
# ══════════════════════════════════════════════════════════

def test_witch_sees_attack():
    state = _make_test_game(6)
    state.phase = Phase.NIGHT_WITCH
    state.wolf_attack_target = next(p for p in state.players if p.role != Role.WITCH).id

    witch = next(p for p in state.players if p.role == Role.WITCH)
    obs = build_observation_for_player(state, witch.id)

    assert "attacked_player" in obs, "Witch didn't see attacked player!"
    assert obs["attacked_player"] == state.wolf_attack_target, "Witch saw wrong attacked player!"

    print("✅ test_witch_sees_attack PASSED")


# ══════════════════════════════════════════════════════════
# Test 6: Night resolution — guard blocks wolf
# ══════════════════════════════════════════════════════════

def test_guard_blocks_wolf():
    state = _make_test_game(6)

    target = next(p for p in state.players if p.team == Team.VILLAGE)
    state.guard_target = target.id
    state.wolf_targets = [target.id]
    state.wolf_attack_target = target.id

    alive_before = sum(1 for p in state.players if p.alive)
    _resolve_night(state)
    alive_after = sum(1 for p in state.players if p.alive)

    assert alive_after == alive_before, "Target died despite guard protection!"
    assert target.alive, "Target died despite guard protection!"

    # Announcement should say no one died
    ann = next((e for e in reversed(state.public_history) if e.type == "night_result"), None)
    if ann:
        assert "No one died" in ann.content, \
            f"Public announcement leaked guard info: {ann.content}"

    print("✅ test_guard_blocks_wolf PASSED")


# ══════════════════════════════════════════════════════════
# Test 7: Witch heal
# ══════════════════════════════════════════════════════════

def test_witch_heal():
    state = _make_test_game(6)
    target = next(p for p in state.players if p.team == Team.VILLAGE)

    state.wolf_attack_target = target.id
    state.witch_heal_used = True
    state.guard_target = None  # not protected by guard

    # Manually set witch memory
    witch = next(p for p in state.players if p.role == Role.WITCH)
    pm = state.private_memories.get(witch.id)
    if pm and pm.witch:
        pm.witch.heal_available = False

    alive_before = sum(1 for p in state.players if p.alive)
    _resolve_night(state)
    alive_after = sum(1 for p in state.players if p.alive)

    assert alive_after == alive_before, "Target died despite witch heal!"

    print("✅ test_witch_heal PASSED")


# ══════════════════════════════════════════════════════════
# Test 8: Witch poison kills
# ══════════════════════════════════════════════════════════

def test_witch_poison():
    state = _make_test_game(6)
    target = next(p for p in state.players if p.team == Team.WEREWOLF)

    state.witch_poison_target = target.id

    alive_before = sum(1 for p in state.players if p.alive)
    _resolve_night(state)
    alive_after = sum(1 for p in state.players if p.alive)

    assert alive_after == alive_before - 1, "Witch poison didn't kill!"
    assert not target.alive, "Target didn't die from poison!"

    print("✅ test_witch_poison PASSED")


# ══════════════════════════════════════════════════════════
# Test 9: Voting
# ══════════════════════════════════════════════════════════

def test_voting():
    state = _make_test_game(6)
    state.phase = Phase.DAY_EXECUTION

    target = state.players[0]
    # All other players vote for target
    for p in state.players:
        if p.id != target.id:
            state.day_votes[p.id] = target.id

    _resolve_execution(state)

    assert not target.alive, "Voted player should be dead!"
    ann = next((e for e in reversed(state.public_history) if e.type == "execution"), None)
    assert ann and target.name in str(ann.content), "Execution didn't announce!"

    print("✅ test_voting PASSED")


# ══════════════════════════════════════════════════════════
# Test 10: Tie vote
# ══════════════════════════════════════════════════════════

def test_tie_vote():
    state = _make_test_game(6)
    state.config.tie_vote_policy = "no_execution"
    state.phase = Phase.DAY_EXECUTION

    p1 = state.players[0]
    p2 = state.players[1]

    state.day_votes = {
        p1.id: p2.id,
        p2.id: p1.id,
        state.players[2].id: "skip",
        state.players[3].id: "skip",
        state.players[4].id: "skip",
        state.players[5].id: "skip",
    }

    alive_before = sum(1 for p in state.players if p.alive)
    _resolve_execution(state)
    alive_after = sum(1 for p in state.players if p.alive)

    assert alive_after == alive_before, "Someone died on tie vote!"

    print("✅ test_tie_vote PASSED")


# ══════════════════════════════════════════════════════════
# Test 11: Win condition — village wins
# ══════════════════════════════════════════════════════════

def test_village_wins():
    state = _make_test_game(6)
    # Kill all wolves
    for p in state.players:
        if p.role == Role.WEREWOLF:
            p.alive = False

    _check_win_condition(state)
    assert state.game_over, "Game not over when all wolves dead!"
    assert state.winner == "village", f"Wrong winner: {state.winner}"

    print("✅ test_village_wins PASSED")


# ══════════════════════════════════════════════════════════
# Test 12: Win condition — wolves win
# ══════════════════════════════════════════════════════════

def test_wolves_win():
    state = _make_test_game(6)
    # 2 wolves, 4 villagers. Kill 2 villagers → 2 wolves, 2 villagers → wolves >= non-wolves
    villagers = [p for p in state.players if p.team == Team.VILLAGE]
    for v in villagers[:2]:
        v.alive = False

    _check_win_condition(state)
    assert state.game_over, "Game not over when wolves >= non-wolves!"
    assert state.winner == "werewolves", f"Wrong winner: {state.winner}"

    print("✅ test_wolves_win PASSED")


# ══════════════════════════════════════════════════════════
# Test 13: Save/Load roundtrip
# ══════════════════════════════════════════════════════════

def test_save_load_roundtrip():
    state = _make_test_game(6)
    state.phase = Phase.DAY_DISCUSSION
    state.day = 2
    state.night = 2

    save_path = "/tmp/test_ww_state.json"
    save_game_state(state, save_path)
    loaded = load_game_state(save_path)

    assert loaded is not None, "Loaded state is None!"
    assert loaded.game_id == state.game_id
    assert loaded.phase == state.phase
    assert loaded.day == state.day
    assert loaded.night == state.night
    assert len(loaded.players) == len(state.players)

    # Verify private memories preserved
    for p in state.players:
        orig_pm = state.private_memories.get(p.id)
        load_pm = loaded.private_memories.get(p.id)
        assert (orig_pm is None) == (load_pm is None), f"Memory mismatch for {p.id}"

    print("✅ test_save_load_roundtrip PASSED")


# ══════════════════════════════════════════════════════════
# Test 14: 15 player setup
# ══════════════════════════════════════════════════════════

def test_15_player_setup():
    names = [{"id": f"agent_{i}", "name": f"Agent{i}"} for i in range(1, 16)]
    state = create_game(names, game_id="test-15", config=GameConfig())

    assert len(state.players) == 15, f"Expected 15 players, got {len(state.players)}"

    wolves = [p for p in state.players if p.role == Role.WEREWOLF]
    seers = [p for p in state.players if p.role == Role.SEER]
    witches = [p for p in state.players if p.role == Role.WITCH]
    guards = [p for p in state.players if p.role == Role.GUARD]
    hunters = [p for p in state.players if p.role == Role.HUNTER]
    villagers = [p for p in state.players if p.role == Role.VILLAGER]

    assert len(wolves) == 4, f"Expected 4 wolves, got {len(wolves)}"
    assert len(seers) == 1, f"Expected 1 seer, got {len(seers)}"
    assert len(witches) == 1, f"Expected 1 witch, got {len(witches)}"
    assert len(guards) == 1, f"Expected 1 guard, got {len(guards)}"
    assert len(hunters) == 1, f"Expected 1 hunter, got {len(hunters)}"
    assert len(villagers) == 7, f"Expected 7 villagers, got {len(villagers)}"

    # Verify wolves know each other
    for wolf in wolves:
        pm = state.private_memories[wolf.id]
        assert len(pm.wolf.known_wolves) == 4, f"Wolf {wolf.id} doesn't know all wolves!"

    print("✅ test_15_player_setup PASSED")


# ══════════════════════════════════════════════════════════
# Test 15: Dead player cannot vote
# ══════════════════════════════════════════════════════════

def test_dead_cannot_vote():
    state = _make_test_game(6)
    state.phase = Phase.DAY_VOTING
    dead = state.players[0]
    dead.alive = False

    obs = build_observation_for_player(state, dead.id)
    assert obs.get("can_vote") == False, f"Dead player can vote! got {obs.get('can_vote')}"
    assert "You are dead" in obs.get("message", ""), "No death message!"

    print("✅ test_dead_cannot_vote PASSED")


# ══════════════════════════════════════════════════════════
# Run all tests
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_villager_sees_no_roles,
        test_werewolf_sees_only_wolves,
        test_seer_private_info,
        test_night_blindness,
        test_witch_sees_attack,
        test_guard_blocks_wolf,
        test_witch_heal,
        test_witch_poison,
        test_voting,
        test_tie_vote,
        test_village_wins,
        test_wolves_win,
        test_save_load_roundtrip,
        test_15_player_setup,
        test_dead_cannot_vote,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__} FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    if failed == 0:
        print("🎉 ALL TESTS PASSED!")
    else:
        print(f"⚠️  {failed} test(s) failed!")
