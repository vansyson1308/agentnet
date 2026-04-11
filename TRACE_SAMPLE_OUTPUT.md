# Trace Sample Outputs

The dashboard resolves raw API tracing streams into tree representations for visualization. Examples of what the data looks like prior to render:

## Full Success Flow Example (Structured)

```json
[
  {
    "span_id": "aa11-bb22",
    "parent_span_id": null,
    "agent_id": "caller-abc",
    "event": "task_created",
    "capability": "summarize",
    "status": "success",
    "duration_ms": null,
    "credits_used": null,
    "created_at": "2026-04-11T09:00:00.000000",
    "extra_data": {},
    "children": [
      {
        "span_id": "bb33-cc44",
        "parent_span_id": "aa11-bb22",
        "agent_id": "callee-xyz",
        "event": "task_started",
        "capability": "summarize",
        "status": "success",
        "duration_ms": null,
        "credits_used": null,
        "created_at": "2026-04-11T09:00:00.500000",
        "extra_data": {},
        "children": [
          {
            "span_id": "cc55-dd66",
            "parent_span_id": "bb33-cc44",
            "agent_id": "callee-xyz",
            "event": "task_completed",
            "capability": "summarize",
            "status": "success",
            "duration_ms": 1250,
            "credits_used": 15,
            "created_at": "2026-04-11T09:00:01.750000",
            "extra_data": {},
            "children": []
          }
        ]
      }
    ]
  }
]
```
*Visual Result*: The UI draws a three-tier waterfall stepping down, all showing green SUCCESS badges. Duration bounds itself cleanly to the final block.

## 2. Aborted / Failure Flow Example

```json
[
  {
    "span_id": "error-11",
    "parent_span_id": null,
    "agent_id": "caller-abc",
    "event": "task_created",
    "status": "success",
    "created_at": "2026-04-11T09:05:00.000000",
    "children": [
      {
         "span_id": "error-22",
         "parent_span_id": "error-11",
         "agent_id": "callee-xyz",
         "event": "task_failed",
         "status": "failed",
         "duration_ms": 110,
         "created_at": "2026-04-11T09:05:00.110000",
         "extra_data": {
           "error_message": "LLM Provider Timeout Exceeded."
         },
         "children": []
      }
    ]
  }
]
```
*Visual Result*: The frontend catches the nested `task_failed` span. It borders the inner box red, applies the red FAILED badge, and unpacks the `error_message` dict value into a harsh mono-spaced payload block directly underneath the generic properties so human ops can isolate the context instantly.

## 3. The Partial (In Progress) Trace Example

```json
[
  {
    "span_id": "partial-99",
    "parent_span_id": null,
    "agent_id": "caller-abc",
    "event": "task_created",
    "status": "success",
    "created_at": "2026-04-11T09:10:00.000000",
    "children": []
  }
]
```

*Visual Result*: If the Callee node completely disappears from the P2P connection due to an ungraceful reboot, the trace stays stranded with a singular root span block. UI successfully ignores building downstream components due to missing `task_started` dependencies. Operators understand immediately the remote end never triggered the protocol.
