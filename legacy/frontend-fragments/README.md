# legacy/frontend-fragments — NOT BUILT, NOT SERVED

Orphaned UI code moved out of the repository root in Phase 2.5 so it cannot be mistaken
for a second dashboard:

* `src/` — partial React SPA (duplicated `.js`/`.jsx` components, no `package.json`,
  no build config, API base hard-coded to a retired hostname).
* `app/`, `dashboard/`, `routes/`, `static/`, `dashboard_timeline.html` — FastAPI/Jinja
  fragments that import a non-existent `app.config`; nothing mounts them.

The canonical UI is the Flask dashboard in `services/dashboard/` (served by
`docker-compose.yml` on port 8080). A future SPA starts from scratch against `/v1/*`.
