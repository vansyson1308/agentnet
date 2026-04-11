# Trace Data Model (Wave 5)

## Raw API Shape

The backend endpoint `GET /v1/tasks/traces/{trace_id}` returns a JSON payload representing a distributed trace of execution.

```json
{
  "trace_id": "uuid-string",
  "total_spans": 3,
  "spans": [
    {
      "id": "uuid-string",
      "trace_id": "uuid-string",
      "span_id": "uuid-string",
      "parent_span_id": "uuid-string | null",
      "agent_id": "uuid-string",
      "event": "task_created | task_started | task_completed | task_failed",
      "capability": "echo",
      "duration_ms": 120,
      "status": "success | failed | null",
      "credits_used": 100,
      "extra_data": { 
        "error_message": "optional details" 
      },
      "created_at": "2026-04-11T00:00:00.000Z"
    }
  ]
}
```

## Normalized Shape for the Dashboard

To render a waterfall effectively using Jinja, the dashboard (in `main.py`) processes the raw flat list of spans into a hierarchical **Span Tree**. Each node contains its raw span dictionary with a new `children` list key.

### Normalization Logic

1. Create a `span_map` dictionary, keyed by `span_id`.
2. Iterate through all raw `spans`: add an empty `children` array to each.
3. Iterate again:
    - If `parent_span_id` exists and is present in `span_map`, append this span to its parent's `children`.
    - If `parent_span_id` is null or missing from the map (orphans in partial traces), mark it as a "root node".

### Normalized Tree Structure

```python
[
  {
    "span_id": "...",
    "parent_span_id": None,
    "event": "task_created",
    "status": "success",
    "children": [
      {
        "span_id": "...",
        "parent_span_id": "...",
        "event": "task_started",
        "status": "success",
        "children": [
          {
            "span_id": "...",
            "parent_span_id": "...",
            "event": "task_completed",
            "status": "success",
            "children": []
          }
        ]
      }
    ]
  }
]
```

## Assumptions & Edge Cases

1. **Ordering**: The backend already orders flat spans by `created_at`. Sorting by time within children guarantees timeline accuracy.
2. **Missing Parents**: If a span's parent does not exist in the dataset (due to data loss, asynchronous errors, or deletion), it gracefully becomes a root node so the UI does not crash or lose visibility of the data.
3. **Empty Traces**: A task might have a `trace_id` but zero spans logged if the system crashed instantly. Handled securely by rendering an "Insufficient trace data" empty state.
4. **Duration Defaults**: If `duration_ms` is null (e.g., event just started/did not finish duration tracking), it visually defaults to `-`.
