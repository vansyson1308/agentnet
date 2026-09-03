# Society code candidates

Documentation candidates produced by the autonomous engineering loop
(Society_Architect → Society_Builder → Society_QA) land in this directory,
one Markdown file per candidate, on an `agentnet-auto/<candidate-id>` branch.

Nothing here is merged by the runtime. A human reviews the branch, and the
candidate's durable status lives in the `code_candidates` table
(`GET /v1/society/candidates`).

Required structure (verified mechanically by
`tests/society/acceptance/test_candidate_docs.py`):

```
# <Title>

## Problem
## Proposed change
## Evidence
## Verification
```
