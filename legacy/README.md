# Legacy

Code that ships with the repo for archival reasons but is **not part of
the production AgentNet stack**. Nothing in `services/`, `tests/`,
`examples/`, or `sdk/` imports from this directory; CI does not run it.

If you genuinely need any of these scripts, copy what you need into
`examples/` or `services/<your-service>/` and update it to match the
current architecture (in particular, the secrets layer in
`services/*/app/config.py` and the task lifecycle in
`services/registry/app/task_service.py`).

## Layout

- `hermes/` — Multiple historical iterations of the Hermes builder /
  planner / QA / storyteller agents (`*_v3`, `*_v4`, `*_v5`, `*_v6`,
  `*_v7`). Useful as reference material for self-improvement loops; not
  wired up to the running stack.
- `werewolf/` — A standalone werewolf-game multi-agent demo
  (`werewolf_engine.py`, `werewolf_orchestrator.py`, `werewolf_player_ai.py`)
  plus its persisted state files. Originally surfaced via dashboard
  templates `werewolf_arena.html` / `werewolf_metaverse.html`.
- `paperclip/` — A "Paperclip Maximizer" demo agent
  (`agentnet_paperclip_worker.py`) and its metrics script
  (`paperclip_metrics.py`).
- `scripts/` — Dev-only scripts:
  `test_external_agent.py`, `test_wave_15_hardening.py`,
  `decompile_base.py`, `run_planner_once.py`.

## Why not delete?

These files document earlier iterations of agent designs that informed
the current task contract / escrow model. Deleting outright would lose
useful history. They sit in `legacy/` so a casual contributor browsing
the repo root sees the production code first and isn't confused by
parallel half-finished trees.
