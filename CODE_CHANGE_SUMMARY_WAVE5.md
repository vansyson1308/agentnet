# Wave 5 Code Changes

## Files Modified

### `services/dashboard/app/api_client.py`
- Injected `get_trace(self, trace_id)` to execute the `HTTP GET` onto `/v1/tasks/traces/{trace_id}` using existing human web-session headers for API-layer safety.

### `services/dashboard/app/main.py`
- Appended `@app.route("/tasks/<task_id>/trace")`, implementing the Trace Page view logic.
- Implemented a tree-normalization algorithm to format flat span vectors originating from OpenTelemetry-backed APIs into cohesive parent/child hierarchy mappings utilizing a `span_map` mapping layer.

### Previous Templates
- `tasks.html`: Mounted the `url_for('task_trace_page')` router across each task summary entry for direct "Trace" pathway access.
- `task_status.html`: Installed the global "View Execution Trace" bridge button.

## Files Created

### `services/dashboard/app/templates/task_trace.html`
- A brand new Jinja structure designed strictly for span trees.
- Leveraged Jinja macros for recursive deep-dive rendering (`{% macro render_span(node, depth) %}`).
- Encoded color styling logic mapping directly to standard OpenTelemetry semantic states (`success`, `failed`).
- Mapped error output text explicitly to unformatted `monospace` fonts to ensure logs remain safely viewable.
