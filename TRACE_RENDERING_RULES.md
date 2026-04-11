# Trace Rendering Rules

The `task_trace.html` template dictates how telemetry information visually materializes on the storefront panel.

## 1. Span Presentation Hierarchy
A Span operates as the fundamental bounding box. By default, it presents:
- High-level `<H4>` marking the **Event Type** (`task_created`, `task_started`, `model_invoked`).
- **Timestamp Container**: Standard ISO format cropped linearly to 19 characters (`YYYY-MM-DD HH:MM:SS`), rendering timezone agnostic logs safely.
- **Latency / Cost Sidebar**: Rendered flush right, capturing exactly the duration latency in milliseconds plus implicitly spent network Credits tracking token costs.
- **Core Meta-details block**: Bounding `agent_id` context logs and optional target capabilities directly to hyperlinked tags so users can recursively investigate the worker nodes.

## 2. Nesting Logistics
- The `depth` tracks nesting depth continuously.
- **Inline margin scaling**: Every depth layer compounds a `margin-left: 24px` to the child row dynamically via Jinja compilation.
- **Visual Branching**: The vertical cascade effect is visually tethered using a fixed left border (solid tracking line) interacting with an absolute positioned horizontal 2px line pushing rightward toward the box.

## 3. Structural Color States & Status Indicators
| Status Name | Border Stroke | Background Fill | Status Badge | Description |
|---|---|---|---|---|
| `success` | `#bbf7d0` (Green-200) | White | `<span class="badge-success">SUCCESS</span>` | Operation concluded efficiently and output was recorded. |
| `failed` | `#fecaca` (Red-200) | White | `<span class="badge-danger">FAILED</span>` | An asynchronous violation or exception triggered an early abort. |
| `[progress/none]` | `#fde68a` (Amber-200) | White | `<span class="badge-warning">IN PROGRESS</span>` | Action commenced. Callee signal unresolved. Baseline default. |

## 4. Duration Metrics Calculation
- Backend data stores it exactly as `span.duration_ms`. 
- Rendering logic relies on conditional null-checks: `{{ node.duration_ms ~ ' ms' if node.duration_ms is not none else '-' }}`. This avoids rendering `None ms` to operators. 

## 5. Trace Failure Visibility
- Span architectures rely entirely on explicit exposure if things break. 
- A Jinja gate checks `{% if node.extra_data and node.status == 'failed' %}`. If met, it pops a raw blockquote object colored red, printing verbatim standard error pipelines retrieved straight from the internal `.extra_data.error_message`. Formatting strips away to monospaced text to bypass markdown rendering attacks or visual noise, providing true JSON or standard console logging views.
