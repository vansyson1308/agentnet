# GitHub repository protection — owner action

`main-ruleset.json` is the exact ruleset body for `POST /repos/vansyson1308/agentnet/rulesets`
(GitHub REST API 2022-11-28). It was authored in Phase 3.1 from the live CI job names of the
successful `main` run and could **not** be applied from the engineering session: the session's
GitHub proxy refuses write access to the rulesets API path (HTTP 403), and `main` currently has
**no ruleset and no branch protection** (`GET /repos/vansyson1308/agentnet/rulesets` → `[]`).

## MAIN RULESET — OWNER ACTION REQUIRED

Run once with an account that administers the repository (a classic or fine-grained token with
**Administration: write**, or `gh auth login` as the owner):

```bash
gh api --method POST -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
  /repos/vansyson1308/agentnet/rulesets --input deploy/github/main-ruleset.json
# verify
gh api /repos/vansyson1308/agentnet/rules/branches/main
```

Or in the UI: *Settings → Rules → Rulesets → New branch ruleset*, target **Default branch**,
enforcement **Active**, bypass list **empty**, and enable exactly:

| Rule | Setting |
| --- | --- |
| Restrict deletions | on |
| Block force pushes | on |
| Require a pull request before merging | required approvals **0** (single-owner repository; AgentNet's own promotion policy enforces human approval for RED changes), dismiss stale approvals on push, require conversation resolution, allowed merge methods squash/merge/rebase |
| Require status checks to pass | **Require branches to be up to date before merging** on; checks (source: GitHub Actions, app id 15368): `Lint (runtime-failure classes are fatal)`, `Dependency audit (pip-audit)`, `Per-service isolated environments (pip check + import smoke on the images' Python 3.10)`, `Unit + PostgreSQL-backed + Society + migrations + compose topology`, `Fresh install → migrate → run → E2E → restart (persistence)`, `Images build + compose projects render` |

Rules that must stay **off/empty**: any bypass actor (never the Society GitHub App, never the
promotion controller, never a deploy key), "Require signed commits" (would block the web squash
merge), "Require linear history" (optional; squash merges satisfy it either way), merge queue.

The check names are the `name:` of the six jobs in `.github/workflows/ci.yml`; if a job is renamed,
update the ruleset in the same change or `main` locks. AgentNet's code refuses direct/force pushes
to `main` and autonomous merges regardless of this ruleset
(`services/registry/app/society/promotion.py`, `promotion_github.py`); the ruleset is defence in
depth, not the control.
