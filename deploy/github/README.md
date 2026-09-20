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

---

## PRODUCTION RULESET — OWNER ACTION REQUIRED (Phase 7 §16)

`production-ruleset.json` protects the `production` branch — the branch Railway's production
environment deploys. It is the same six required checks as `main`, which is only possible because
Phase 7 added `production` to the CI workflow's `push` and `pull_request` triggers: without that,
a push to `production` produces no check suite, every required check sits pending forever, and
Railway's Wait for CI has nothing to wait for (ADR-0008 D3).

```bash
gh api --method POST -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
  /repos/vansyson1308/agentnet/rulesets --input deploy/github/production-ruleset.json
# verify
gh api /repos/vansyson1308/agentnet/rules/branches/production
```

Two settings differ from `main`, deliberately:

| Setting | `main` | `production` | Why |
| --- | --- | --- | --- |
| `do_not_enforce_on_create` | `false` | **`true`** | The branch does not exist yet. With enforcement on create, the ruleset would make its own bootstrap impossible. Because "Restrict deletions" and "Block force pushes" are both on, the branch can be created exactly **once** — so this exemption can only ever apply to the initial trusted bootstrap, and never again. |
| `allowed_merge_methods` | squash, merge, rebase | **squash, merge** | Rebase rewrites the commits, and the release gate's last act before handing over is `verify_tree_equality` — the release branch's tree must equal the approved target's tree. A release whose tree no longer matches the SHA that was approved is not the change that was approved. |

Everything else is identical and equally non-negotiable: **bypass actors empty** — in particular the
Society GitHub App is *not* a bypass actor on `production` any more than on `main`, and there is no
deploy key. The Society has no production authority at all: it cannot open a PR into `production`
(`.railway/production.ts` declares no society-worker; `settings.production_deploy_enabled` is a hard
`False`), so this ruleset is not what stops it — it is defence in depth behind a code-level boundary
that `tests/test_production_release_gate.py` pins.

Order matters: apply this ruleset **after** the initial trusted bootstrap creates `production`, or
accept that `do_not_enforce_on_create: true` is what lets the bootstrap through if applied before.
Either order works; applying it after is the one that needs no exemption at all.
