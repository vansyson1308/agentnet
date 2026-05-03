#!/usr/bin/env python3
"""
Werewolf v2 — Auto Rotation Runner with DeepSeek LLM
=====================================================
Runs games continuously with DeepSeek LLM agents.
After each game ends, automatically starts a new game with
role rotation (roles are randomly re-shuffled every game).

Usage:
  python3 auto_rotation.py [--games N] [--delay SEC]

State saved to: /opt/agentnet/werewolf_data/game_v2_state.json
Transcripts:    /opt/agentnet/werewolf_data/transcript_<game_id>.json
"""
import json
import sys
import os
import time
import argparse

# ── Load DeepSeek API key from Hermes auth.json ──
_auth_paths = [
    "/root/.hermes/auth.json",
    os.path.expanduser("~/.hermes/auth.json"),
]
for _ap in _auth_paths:
    if os.path.exists(_ap):
        with open(_ap) as _f:
            _auth_data = json.load(_f)
        _ds = _auth_data.get("credential_pool", {}).get("deepseek", [])
        if _ds:
            os.environ["DEEPSEEK_API_KEY"] = _ds[0]["access_token"]
            break

if not os.environ.get("DEEPSEEK_API_KEY"):
    print("❌ Could not load DeepSeek API key from Hermes auth.json")
    print("   Check: ~/.hermes/auth.json → credential_pool.deepseek[0].access_token")
    sys.exit(1)

# ── Import game modules ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cron_game import run_game, AGENT_POOL, USE_LLM


def main():
    parser = argparse.ArgumentParser(description="Werewolf v2 Auto Rotation")
    parser.add_argument("--games", type=int, default=1, help="Number of games to run (default: 1)")
    parser.add_argument("--delay", type=int, default=15, help="Delay between games in seconds (default: 15)")
    parser.add_argument("--quiet", action="store_true", help="Less verbose output")
    args = parser.parse_args()

    print("=" * 70)
    print("🐺 WEREWOLF ARENA v2 — AUTO ROLE ROTATION")
    print(f"   Agents: DeepSeek LLM ({'REAL' if USE_LLM else 'MOCK'})")
    print(f"   Players: {len(AGENT_POOL)}")
    print(f"   Games: {args.games}")
    print("=" * 70)

    results = []
    for g in range(1, args.games + 1):
        print(f"\n{'#' * 70}")
        print(f"# GAME {g}/{args.games} — Role rotation active")
        print(f"{'#' * 70}\n")

        t0 = time.time()
        state = run_game()
        elapsed = time.time() - t0

        winner = state.winner or "draw"
        alive = [p for p in state.players if p.alive]
        results.append({
            "game_id": state.game_id,
            "winner": winner,
            "alive_count": len(alive),
            "duration_s": round(elapsed),
        })

        print(f"\n📊 Game {g} complete in {elapsed:.0f}s — {winner.upper()} wins")

        if g < args.games:
            print(f"⏳ Next game in {args.delay}s (roles re-shuffled)...")
            time.sleep(args.delay)

    # Summary
    print(f"\n{'=' * 70}")
    print(f"📊 SUMMARY: {args.games} GAME(S) COMPLETE")
    print(f"{'=' * 70}")
    for r in results:
        print(f"  {r['game_id']:30s} | 🏆 {r['winner']:12s} | {r['alive_count']} survivors | {r['duration_s']}s")
    print()

    return results


if __name__ == "__main__":
    main()
