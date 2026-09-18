# ADR-0005 — Pre-deploy boundary closure (Phase 3.1)

Status: accepted (2026-09-18) · Supersedes nothing; refines ADR-0004 D4 (promotion provider).

## Context

Phase 3 merged the self-development foundation (`d4008fe`). Four boundary defects were found
afterwards, all of which had to be closed before any host, credential or GitHub App is connected:

1. a **second, legacy self-improvement control plane** was still active — the general worker's
   reflection loop generated `ImprovementProposal` rows from failed tasks and its backlog bridge
   rewrote every `PROPOSED` proposal (including Society ones) into `AGENT_BACKLOG.md`, flipping
   them to `CONVERTED_TO_TASK` before the Governor could review them;
2. **synthetic activity** (`agents/poll_agent.py`: one task every 30 s "to create activity") sat in
   the active tree as if it were a production agent — a fabricated failure rate would have driven
   real Scout proposals through the Phase-3 telemetry;
3. the real GitHub promotion provider built `https://x-access-token:<TOKEN>@github.com/...` and
   passed it in **`git push` argv** — visible to `ps`, crash dumps and any subprocess error;
4. the **staging configuration contract** still exposed only Phase-2 model variables, and one
   comment claimed the image baked `--forwarded-allow-ips '*'` (it does not).

Constraints: no host, DNS, model credential, GitHub App or installation token exists; nothing may
be asked from the user; auto-merge stays OFF; production Society stays OFF; A2A is out of scope.

## Decisions

### D1 — One autonomous improvement control plane: the Society runtime

The worker no longer imports or calls `run_reflection_loop` / `convert_proposals_to_backlog`; the
module is archived as `legacy/hermes/worker_reflection_loop.py` and the file backlog as
`legacy/hermes/AGENT_BACKLOG.md`. World ingestion (`society/world.py`) already turns failed and
timed-out tasks into `task.failed`/`task.timeout` events, the Scout proposes with structured
evidence, and open proposals are deduplicated by title (`_create_improvement`). The dashboard's
preview helper (`registry/app/reflection.py`, pure, no writes) stays. `REFLECTION_LOOP_*` and
`AGENT_BACKLOG_PATH` are removed from `.env.example` and the local compose file.

*Historical rows*: proposals that reached `CONVERTED_TO_TASK` only because of the retired bridge
are left untouched — the status is legitimate for rows whose `converted_task_id` is set by the
Society executor, and rewriting user data was not necessary for correctness. Operators can tell
the two apart: bridge conversions have `converted_task_id IS NULL`.

Regression: `tests/society/test_single_control_plane.py` (structural + the real worker loop ticking
over a Society proposal that must stay `PROPOSED`; three identical failures → one open workstream)
and the no-legacy-side-effect block in `tests/society/test_e2e_self_development.py`.

### D2 — Synthetic agents are legacy fixtures

`agents/{poll,echo,storyteller}_agent.py` → `legacy/synthetic-agents/` with a README; the
storyteller's hard-coded Redis URL was replaced by `REDIS_URL` from the environment even in the
archive. No compose file, deploy script, Makefile target or runtime module references them
(`test_single_control_plane.py::test_no_deployment_or_script_starts_legacy_activity`).
Deterministic test fixtures (`tests/society/fixtures/`, the scripted model) are unrelated and stay.

### D3 — The staging contract is the Phase-3 runtime contract

`docker-compose.staging.yml` exposes every externally necessary Phase-3 setting for the society
worker with safe defaults (`SOCIETY_RUNTIME_ENABLED=false`, `SOCIETY_AUTONOMOUS_CODE_ENABLED=false`,
`SOCIETY_PROMOTION_PROVIDER=disabled`, `SOCIETY_AUTO_MERGE_ENABLED="false"` not overridable,
`SOCIETY_DEPLOYMENT_PROVIDER=disabled`, `SOCIETY_STAGING_DEPLOY_ENABLED="false"`). Secrets are never
compose literals: `SOCIETY_GITHUB_TOKEN`, `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM` and the DeepSeek key
do not appear in any compose file; the App private key is a platform-mounted file whose PATH is
passed through. `tests/test_config_parity.py` keeps `SocietySettings`, `.env.example`, the staging
file and `docs/DEPLOYMENT_ARCHITECTURE.md` in sync and rejects the stale proxy claim.

### D4 — `git push` authenticates through `GIT_ASKPASS`, never a URL or argv

`society/promotion_github.py` pushes to `https://github.com/<owner>/<repo>.git` (no credential in
the URL) with `git -c credential.helper= push --no-verify <url> HEAD:refs/heads/<branch>`. The
credential reaches git only through a temporary helper (`GIT_ASKPASS`, mode 0700, no secret
embedded, removed in `finally`) that echoes two variables that exist only in the child process
environment. Credential helpers are disabled for the call (empty `credential.helper` resets the
list — gitcredentials(7)), `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, no shell, nothing is
written to `.git/config` or any credential store, stderr is scrubbed before it can reach an
exception, and force/mirror/delete flags and `+refspec` are structurally impossible. Proven with a
recording runner and against a real `git push` to a local smart-HTTP server with Basic auth
(`tests/society/git_http_harness.py`, `git http-backend`).

### D5 — A credential provider boundary with an in-memory App token lifecycle

`society/github_credentials.py` defines `GitHubCredentialProvider` with `disabled` (default),
`static` (`SOCIETY_GITHUB_TOKEN` read at call time; tests / temporary operator use) and `app`.
`GitHubAppCredentialProvider` signs an RS256 JWT (`iat` = now − 60 s, `exp` = `iat` + 10 min,
`iss` = App ID) with the private key read from `SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE` (preferred;
platform-mounted) or, only where a mount is impossible, `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM`; the key
is read at signing time and dropped. It exchanges the JWT at
`POST /app/installations/{id}/access_tokens` with `repositories=[<repo>]` and the minimum
`permissions`, parses `expires_at` (a missing value is treated as five minutes, never an hour),
caches the token in process memory under a lock (single-flight), reuses it while
`now + SOCIETY_GITHUB_TOKEN_REFRESH_MARGIN_SECONDS < expires_at`, refreshes it otherwise,
invalidates it on a 401 from any GitHub call and retries that call exactly once, and never assumes a
token length or prefix (GitHub began rolling out the stateless `ghs_APPID_JWT` format on
2026-04-27). Nothing is serialised: `repr`/`str` redact, exceptions carry status codes only, no DB
table exists for tokens. The model never receives a provider instance.

### D6 — Minimum Society App permissions

Contents read/write, Pull requests read/write, Checks read, Metadata read. Not Administration,
Issues, Workflows, Secrets or Actions write. The installation token is downscoped to exactly these
even if the App registration is broader. The App must not be a ruleset bypass actor.

### D7 — `main` protection is defence in depth, code refusals are the control

The controller refuses `main`/`master`/the configured base, any branch outside
`agentnet-auto/`, any non-fast-forward push and any merge while `SOCIETY_AUTO_MERGE_ENABLED` is
false — regardless of repository settings (`tests/society/test_promotion.py`,
`test_github_credentials_and_push.py`). A GitHub ruleset on the default branch (require pull
request, required status checks = the six CI jobs with strict up-to-date policy, block force pushes,
block deletion, zero bypass actors) is the second layer. Whether it could be created from this
session is recorded in `docs/GITHUB_PROMOTION.md` ("Main ruleset"); if the API refuses for lack of
administration permission, the exact owner action is documented instead of weakening anything.

## Official documentation consulted (verified 2026-09-18)

`docs.github.com` and `git-scm.com` are blocked by this environment's egress policy; the same
content was read from the published sources (`github/docs` and `git/git` on
raw.githubusercontent.com) and the GitHub REST OpenAPI description (`github/rest-api-description`).

| Source | What was verified | Used in |
| --- | --- | --- |
| github/docs `apps/.../generating-a-json-web-token-jwt-for-a-github-app.md` | RS256 only; claims `iat` (60 s in the past recommended), `exp` (≤ 10 min), `iss` (App ID or client ID); `Authorization: Bearer <JWT>` | D5 |
| github/docs `apps/.../generating-an-installation-access-token-for-a-github-app.md` + reusables | `POST /app/installations/{id}/access_tokens`; `repositories` / `repository_ids` / `permissions` downscoping (never beyond the installation's grant); tokens expire after 1 hour; **stateless `ghs_APPID_JWT` format rolling out since 2026-04-27 — do not assume 40 characters** | D5 |
| github/rest-api-description `POST /app/installations/{installation_id}/access_tokens` | responses 201/401/403/404/422; expired token → `401 Unauthorized`; response fields `token`, `expires_at`, `permissions`, `repositories` | D5 |
| github/docs `apps/.../managing-private-keys-for-github-apps.md` | PEM (PKCS#1) download; up to 25 keys for rotation; keys never expire, revoke manually; store in a sign-only key vault or, weaker, an environment variable; never hard-code | D5 |
| github/docs `apps/.../choosing-permissions-for-a-github-app.md` | permissions are granted per App and checked per endpoint; request the minimum | D6 |
| git/git `Documentation/gitcredentials.adoc` | `GIT_ASKPASS` program is invoked with a prompt and read from stdout; `core.askPass`; credential helpers; an empty `credential.helper` value resets the helper list | D4 |
| github/docs `repositories/.../available-rules-for-rulesets.md` | require pull request (review count may be 0), required status checks with strict ("up to date") vs loose policy and optional expected source, block force pushes, restrict deletions, bypass actors | D7 |
| github/rest-api-description `POST /repos/{owner}/{repo}/rulesets` | body: `name`, `target`, `enforcement` (`active`/`disabled`/`evaluate`), `bypass_actors`, `conditions.ref_name.include` (`~DEFAULT_BRANCH`), `rules[]` (`pull_request`, `required_status_checks{strict_required_status_checks_policy, required_status_checks[{context, integration_id?}]}`, `non_fast_forward`, `deletion`) | D7 |

## Consequences

* One planner. The general worker keeps timeouts/refunds, daily resets, reputation, presence,
  card crawling, simulation reconciliation and graceful shutdown (`tests/test_worker_lifecycle.py`).
* No token ever appears in git argv, URLs, `.git/config`, logs, events, model context or the
  database; the future App path needs only `SOCIETY_GITHUB_APP_ID`, `SOCIETY_GITHUB_INSTALLATION_ID`
  and a mounted PEM file. `REAL SOCIETY GITHUB PROMOTION: NOT RUN` remains true.
* No schema change: credential caching is process memory only.
* The optional real-GitHub canary and the ruleset creation are the only steps that touch GitHub;
  both use the developer identity of this session, never a Society identity, and neither pushes to
  `main`.
