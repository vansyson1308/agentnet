"""Daily scheduled bench on main (deterministic; the controller calls no model).

Once per UTC day, from ``SOCIETY_BENCH_DAILY_HOUR_UTC`` on, the society-worker
starts ``deploy/railway/bench_live.py`` from a fresh clone of main in a detached
child process: best-of-``SOCIETY_BENCH_DAILY_SAMPLES``, x``SOCIETY_BENCH_DAILY_REPEAT``,
capped at ``SOCIETY_BENCH_DAILY_BUDGET_USD``. The child writes the day's
``society_bench_reports`` row (the trend on /v1/society/status). A
``bench.daily_started`` event marks the day (idempotent). Off unless
``SOCIETY_BENCH_DAILY_ENABLED``. The child gets an ALLOWLISTED environment: the
model and database settings it needs, never a GitHub, release or signing credential.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import SocietySettings
from .events import EventType, emit_event

_PASS = re.compile(r"^(PATH|HOME|LANG|LC_\w+|PYTHON\w*|SSL_CERT_\w+|REQUESTS_CA_BUNDLE|HTTPS?_PROXY|NO_PROXY|ENVIRONMENT|SOCIETY_MODEL_\w+|POSTGRES_\w+|MAINTENANCE_\w+|BENCH_\w+)$")
_CREDENTIAL = re.compile(r"(TOKEN|SECRET|PASSWORD|_KEY|CREDENTIAL|ATTESTATION)")
_ALLOWED_CREDENTIALS = ("SOCIETY_MODEL_API_KEY", "POSTGRES_PASSWORD")


def child_env(settings: SocietySettings, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if _PASS.match(k) and (not _CREDENTIAL.search(k) or k in _ALLOWED_CREDENTIALS)}
    env.pop("BENCH_HARNESS_REF", None)  # the daily run measures main, never a candidate
    env.update({"MAINTENANCE_BUILDER_SAMPLES": str(settings.bench_daily_samples), "BENCH_REPEAT": str(settings.bench_daily_repeat),
                "BENCH_BUDGET_USD": str(settings.bench_daily_budget_usd), "BENCH_SPLIT": "all", "BENCH_PATH": "maintenance"})
    return env


def _spawn(settings: SocietySettings) -> None:  # pragma: no cover - process boundary
    url = f"https://github.com/{settings.github_repository}.git"
    cmd = f"rm -rf /tmp/bench-daily && git clone -q --depth 1 --branch main {url} /tmp/bench-daily && exec python /tmp/bench-daily/deploy/railway/bench_live.py"
    subprocess.Popen(["sh", "-c", cmd], env=child_env(settings), start_new_session=True)  # noqa: S603 -- fixed command, no model input


def maybe_start_daily_bench(db: Session, settings: SocietySettings, *, now: Optional[datetime] = None,
                            spawn: Callable[[SocietySettings], None] = _spawn) -> bool:
    """Start today's bench once (after the configured hour). Commits; True if started."""
    if not settings.bench_daily_enabled or "/" not in settings.github_repository:
        return False
    now = now or datetime.now(timezone.utc)
    key = f"bench-daily:{now.date().isoformat()}"
    if now.hour < settings.bench_daily_hour_utc or db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": key}).first():
        return False
    emit_event(db, event_type=EventType.BENCH_DAILY_STARTED, actor_type="system", idempotency_key=key,
               payload={"day": now.date().isoformat(), "samples": settings.bench_daily_samples, "repeat": settings.bench_daily_repeat,
                        "budget_usd": str(settings.bench_daily_budget_usd)})
    db.commit()
    spawn(settings)
    return True
