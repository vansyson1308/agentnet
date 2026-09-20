# CURRENT_STATE.md consistency check: windows, as-of stamps and cited evidence

## Problem

`CURRENT_STATE.md` is internally inconsistent within a single revision. The
self-development status block states `LIVE MODEL: PROVEN LIVE (deepseek-flash,
2026-09-19) - 57 completed runs, 0 DEAD, 0 retries`, while the status table in
the same file records `175 completed live runs, 0 non-live, 0 DEAD in the final
window`. The same block states `REAL SOCIETY GITHUB APP: NOT YET CONFIGURED`,
while the capability table records the GitHub App credential minting a
correctly scoped installation token (`GITHUB APP READY`).

Both classes of defect share one root cause: the document mixes claims drawn
from different windows and different points in time without labelling either,
and it asserts configuration state without pointing at the line or commit that
evidenced it. A reader cannot tell which number is current, and a reviewer
cannot tell which claim is stale, so the file silently contradicts itself.

## Proposed change

Add a review-time-only consistency check for `CURRENT_STATE.md`, specified as
follows. The check is a review artefact: it reads the document and reports; it
never edits the document, never touches runtime, permissions or deploy, and
never auto-fixes a claim.

1. **Quantitative claims require a window and an as-of timestamp.** Every
   numeric claim about runs, agents, failures, retries or similar counts must
   carry an explicit window (for example `final window`, `last 24h`, or an
   explicit date range) and an as-of timestamp or date. A bare count such as
   `57 completed runs` with no window and no as-of stamp is flagged.
2. **Configuration claims require cited evidence.** Every claim about
   configuration state — GitHub App configured or not, credential provider
   value, feature enabled or disabled — must cite the evidencing preflight line
   or the commit that established it. A claim such as `NOT YET CONFIGURED`
   with no citation is flagged.
3. **Contradiction is a failure.** When a claim contradicts a cited evidence
   line in the same file — for example a status block saying `NOT YET
   CONFIGURED` while a cited preflight line in the same revision says `GITHUB
   APP READY`, or two different completed-run totals presented as current —
   the check fails rather than merely warning.

The deliverable is the reviewable report produced by these three rules, plus
the rule set itself, so a reviewer can see exactly which claim was flagged and
why. The change is deliberately small and self-contained: one new document, no
source edits, no test edits, no runtime surface.

## Evidence

- Approved proposal `d6248715-f4e7-4bea-b320-a63d702c3bcb` (Scout, importance
  38, scope platform) records the contradiction: the status block claims `57
  completed runs` while the capability table claims `175 completed live runs`,
  and the file marks the GitHub App `NOT YET CONFIGURED` while the capability
  table records that credential minting a scoped installation token.
- The same proposal's change statement is the source of the three rules above:
  require an explicit window plus as-of timestamp on every quantitative claim,
  require configuration claims to cite the evidencing preflight line or commit,
  and fail the check when a claim contradicts cited evidence.
- The candidate is bounded to `docs/society/candidates/current-state-consistency-check.md`
  by the architect spec for this candidate, so the check cannot mutate
  `CURRENT_STATE.md` or any runtime file.

## Verification

- `tests/society/acceptance/test_candidate_docs.py` is the acceptance test for
  this candidate: it asserts the first non-empty line is an H1 title, that the
  sections `## Problem`, `## Proposed change`, `## Evidence` and `##
  Verification` are each present with non-empty prose, and that no
  secret-looking string appears in the document.
- The document is review-time only by construction: it specifies a check and
  its failure conditions, and contains no code path that writes to
  `CURRENT_STATE.md`, to runtime state, to permissions or to deploy.
- A reviewer can verify the three rules mechanically against the quoted
  contradiction: the `57 completed runs` claim has no window or as-of stamp,
  the `NOT YET CONFIGURED` claim cites no preflight line or commit, and the two
  claims contradict the cited `GITHUB APP READY` evidence in the same revision.
