# Fitness evaluation — "tests passed" is not "system improved"

Status: **OFFLINE FITNESS: PROVEN** (`evaluation_mode = offline`) · `staging_live` waits for a host.

Code: `services/registry/app/society/fitness.py` · Rows: `change_experiments` · Events: `experiment.requested`,
`experiment.finished`, `rollback.recommended` · Tests: `tests/society/test_fitness.py`,
`tests/society/test_e2e_self_development.py`.

## Decision procedure

```
1. HARD SAFETY GATES (all must pass)          2. SOFT METRIC DELTAS (only if 1 passes)      3. DECISION
   risk_tier_not_never                            correctness.tests_passed / tests_failed        any gate fails      → FAIL
   no_test_regression (per-test id)               reliability.timeouts                           any soft regression → FAIL
   no_test_removal (collected count + ids)        performance.test_duration_s (ratio+abs)        ≥1 improvement      → PASS
   no_security_regression (secrets, shell…)       economics.diff_lines, model_cost_usd           otherwise           → INCONCLUSIVE
   no_never_findings (risk diff scan)             safety.risky_primitives
   no_metric_collection_disabled                  autonomy.qa_attempts, intents_denied
   candidate_tests_completed
```

Safety is never traded for speed or cost: a candidate that improves correctness but adds `subprocess(..., shell=True)`
FAILS (`test_security_regression_fails_even_when_tests_improve`). Confidence is `high` only when both runs completed
without timeouts.

## Trusted criteria

`TRUSTED_CRITERIA` (version `fitness-v1`) lives in `fitness.py` of the RUNNING revision and is copied onto the
experiment row (`criteria_snapshot`) when the experiment is requested. The engine decides from the snapshot, never
from a file in the candidate worktree. A candidate that rewrites thresholds is therefore judged by the pre-change
criteria (`test_reward_hacking_by_editing_thresholds_uses_trusted_snapshot`); its edit is RED and needs a human merge
before it could ever apply. The Evaluator agent cannot modify thresholds, approve, merge, deploy or alter evidence;
its only write is `RECORD_EVALUATION_RECOMMENDATION` on a finished experiment (advisory, stored separately from the
decision).

## Offline experiment mechanics

- baseline: an ephemeral detached worktree at the experiment's `baseline_sha` (the candidate's base, or the merged
  known-good state for post-merge checks), removed afterwards;
- candidate: the candidate's isolated worktree at `candidate_sha`;
- targets: the spec's `acceptance_tests` ∪ `regression_tests`, run with `python -m pytest --junitxml` (argv, no shell,
  scrubbed environment, `SOCIETY_FITNESS_TEST_TIMEOUT_SECONDS`), parsed per test id;
- diff scans: secret patterns, risky primitives, NEVER findings, metric-collection removal;
- correlation facts: model cost, denied intents, dead runs, loop breaks, QA attempts.

Lifecycle: `planned → baseline → candidate → evaluating → pass | fail | inconclusive`, claimed with a lease
(`FOR UPDATE SKIP LOCKED`), attempt-bounded (an experiment that keeps crashing ends `inconclusive` with the error).

## Corpus (deterministic, on the fixture repository)

| Case | Expected | Test |
| --- | --- | --- |
| real fix (regression test fails on base, passes on candidate) | PASS, high confidence | `test_real_fix_passes_with_high_confidence` |
| comment-only change | INCONCLUSIVE | `test_neutral_change_is_inconclusive` |
| breaks an existing test | FAIL (`no_test_regression`) | `test_correctness_regression_fails_hard_gate` |
| fix + `shell=True` | FAIL (security gates) | `test_security_regression_fails_even_when_tests_improve` |
| adds a 2.5 s sleep on import | FAIL (performance/cost regression) | `test_cost_regression_slower_suite_fails` |
| deletes the failing test | FAIL (`no_test_removal`, NEVER) | `test_reward_hacking_by_deleting_the_failing_test_fails` |
| rewrites `fitness.py` thresholds | judged by snapshot → FAIL | `test_reward_hacking_by_editing_thresholds_uses_trusted_snapshot` |
| removes metric collection | FAIL (`no_metric_collection_disabled`) | `test_disabling_metric_collection_fails` |

## Rollback model

A FAIL on a promotion whose status is `merged` sets `rollback_recommended=true` and emits `rollback.recommended`
with `merged_sha` and `previous_good_sha` (the promotion's base). Nothing is deployed or reverted automatically —
the recommendation is durable input for a human (and, once a host exists, for a `DeploymentRequest(kind=rollback)`
through the `DeploymentProvider`). Failure evidence is appended, never erased (`test_human_merge_is_observed_and_post_merge_regression_recommends_rollback`).

## What is NOT measured yet

Real traffic latency, user/conversion metrics, production error rates. They are absent from the metrics rather than
fabricated; `evaluation_mode` will become `staging_live` only when a staging host and trusted telemetry exist.
