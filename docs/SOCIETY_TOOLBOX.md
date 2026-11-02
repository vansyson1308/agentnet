# The Society toolbox: agents build reusable tools

Owner direction, 2026-11-02 ("Hyra: agents develop reusable tools"): the Society should not only
solve one ticket at a time. When the same kind of problem keeps coming back, an agent writes the
method down **once** as a tool, and every later run of the roles it serves starts from it. One
tool-development effort then benefits many later tasks.

Code: `services/registry/app/society/toolbox.py`. Flag: `SOCIETY_TOOLBOX_ENABLED` (default
`false`). Schema: `SOCIETY_TOOLBOX_SQL`, migration `0031_society_toolbox`. Tests:
`tests/society/test_toolbox.py`.

## The loop

| Step | Who | What |
|---|---|---|
| Design | any role (model) | `PROPOSE_TOOL` with `kind` (`workflow`, `prompt`, `checklist`, `toolkit`), `name`, `roles`, `problem`, `steps`, and `occurrences`: at least 2 earlier run, ticket, candidate or proposal ids where the problem happened. A `toolkit` names 1-5 files that are already on main. |
| Upgrade | any role (model) | `REVISE_TOOL` writes the next immutable version of a live tool. A version is never edited in place. |
| Experiment | deterministic | An experimental tool is shown to the runs of its roles in about half of the stories (arm = sha256 of tool id + story id) and withheld in the rest. A revision competes with the version it revises: one arm sees the new version, the other the old one. |
| Evaluate | deterministic | A trial is one completed run with at least one side-effecting intent. It succeeded when every side-effecting intent EXECUTED. With `SOCIETY_TOOLBOX_MIN_TRIALS` trials per arm, a lift of at least 10 points at no more than 1.5x the cost adopts the tool, a drop of 10 points retires it, and 60 trials without a verdict retire it as inconclusive. |
| Reuse | deterministic | An adopted tool is shown to every run of its roles except a 20% holdout. If it falls 10 points behind the holdout it is retired (`regressed`). An adopted revision supersedes the version it revised. |

The Builder's coding harness reads the same tools (its story's arm) as advisory text in the
AuthorPatch input (`build_engine.spec_input`, key `toolbox`).

Each run's context gets a `toolbox` block: the tools assigned to this story (text, labelled
`society_toolbox`), the live library (names, status, uses and wins only) and `recurring`
(rejected candidates by reason and refused intents by type over 14 days, counts of 2 or more).
Which tools were withheld is not shown. It is stored on the run (`context_summary.toolbox_trials`).

## Boundaries

- Tool text is model-authored advisory data. It cannot change a grant, a policy, a budget,
  `files_allowed`, a test or the RED/merge law, and nothing in the toolbox is ever executed.
- There is no "I used the tool" field. Only the execution record of later runs counts.
- A toolkit that does not exist yet is built through the ordinary pipeline (`CREATE_IMPROVEMENT` ->
  plan -> design review -> spec gate -> build -> QA -> PR -> merge law) and registered afterwards.
  The toolbox never files tickets, writes files or runs commands.
- Caps: 3 proposals per agent per day, 6 tools on trial at once, 3 tools shown per run, text up to
  2000 characters, redacted with `secret_redaction` before it is stored.
- The evaluation law (`toolbox.py`, `tests/society/test_toolbox.py`) is owner-merge trusted base
  (`risk.OWNER_MERGE_PATTERNS`, `risk.JUDGE_PATHS`): a candidate cannot change how tools are judged.
- No model call of its own. Cost is only the extra context (at most 3 tools of 2000 characters).

## Operator

- `GET /v1/society/company/toolbox`: every tool with its text and both arms' numbers (operator only).
- `POST /v1/society/company/toolbox/{id}/retire` `{"reason"}`: switch a tool off by hand.

## Turning it on

After the pause ends, set `SOCIETY_TOOLBOX_ENABLED=true` on the staging `society-worker` (the
operator routes read the tables whatever the flag says). The deploy's re-seed adds `PROPOSE_TOOL` and
`REVISE_TOOL` to every role's grant. Optional: `SOCIETY_TOOLBOX_MIN_TRIALS` (default 8, 4..50).
