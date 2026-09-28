"""Process entrypoint of the Maintenance Release Controller (release-control).

    python -m app.maintenance.release_worker

This process holds the ONLY production release credentials (the Maintenance
Release GitHub App key file and a production-scoped Railway token). It must
never hold the model credential: it refuses to start if one is present, and
it never imports cognition, activities or context code
(tests/society/maintenance/test_secret_boundary.py).

Each cycle: run the release controller once, then the kernel watchdog (a
different process watching the kernel).
"""

from __future__ import annotations

import logging
import os
import signal
import time

logger = logging.getLogger("maintenance.release_worker")

#: Variables that must NOT exist in the release-control environment.
FORBIDDEN_ENV = ("SOCIETY_MODEL_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "SOCIETY_GITHUB_TOKEN", "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM")


def startup_problems() -> list:
    return [f"{name} must not be set in release-control (model/Society credentials never share this process)" for name in FORBIDDEN_ENV if os.getenv(name)]


def main() -> None:  # pragma: no cover -- process entrypoint
    from ..config import DATABASE_URL  # noqa: F401 -- fail fast on DB config
    from ..database import SessionLocal
    from ..logging_config import setup_logging
    from .config import get_maintenance_settings
    from .release import ReleaseController
    from .watchdog import check

    setup_logging()
    problems = startup_problems()
    if problems:
        for p in problems:
            logger.error(p)
        raise SystemExit(2)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    ctl = ReleaseController(SessionLocal)
    interval = int(os.getenv("MAINTENANCE_RELEASE_INTERVAL_SECONDS") or "30")
    logger.info("maintenance release controller starting (provider=%s)", ctl.rs.provider)
    while not stop["flag"]:
        try:
            st = ctl.run_once()
            if st.claimed:
                logger.info("release cycle: claimed=%s advanced=%s refused=%s rolled_back=%s", st.claimed, st.advanced, st.refused, st.rolled_back)
            db = SessionLocal()
            try:
                check(db, get_maintenance_settings(), watch="kernel")
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("kernel watchdog failed")
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("release controller cycle failed")
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
