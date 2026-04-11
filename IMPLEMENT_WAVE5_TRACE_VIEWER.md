# Trace Viewer Architecture

## Purpose
The Trace Viewer surfaces the raw backend tracing telemetry generated during an agent-to-agent task execution. By binding the data from `GET /v1/tasks/traces/{trace_id}` to the Human Storefront, operators can debug execution latency and visualize the hop boundaries of distributed LLM actions.

## Data Flow
1. **API Trigger**: A user accesses `/tasks/<task_id>/trace`. The Flask server catches the route and leverages `api_client.get_task(task_id)` to verify the task ownership.
2. **Telemetry Extraction**: The server lifts the `trace_id` out of the task envelope and initiates `api_client.get_trace(trace_id)`.
3. **Data Normalization**: The backend provides spans as a flat, un-nested list. The Dashboard (`app.main`) transforms this flattened array using a `span_map` dictionary layout—detecting `parent_span_id` references to build a nested JSON object structure. Orphans (nodes where the parent wasn't synced) are preserved natively as standalone `root_spans`.
4. **Rendering Context**: The `root_spans` array is passed to `task_trace.html`.

## Waterfall Construction
The waterfall utilizes Server-Side Rendered (SSR) HTML recursion. A Jinja `{% macro %}` iterates over each node. 
- Deeply nested children invoke the same macro, pushing an ever-increasing depth parameter forward. 
- HTML `margin-left` inline bounds are pushed dynamically against the `depth` variable, building visual steps. 
- Vertical tracking lines are generated entirely in CSS via a padded left-border architecture, ensuring no complex JavaScript positioning is needed.

## Failure Handling
Span objects map their states across a spectrum of API failures. When `status == 'failed'`:
- The container frame switches gracefully to a red-bordered, tinted alert background (`#fef2f2`).
- A `FAILED` badge mounts centrally attached to the event tag.
- The `node.extra_data.error_message` is automatically unpacked and exposed at the base of the block, formatted monotonically to isolate traceback details efficiently in real-time.
