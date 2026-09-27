"""Structural, deterministic incident fingerprints (ADR-0010 D4).

A fingerprint identifies "the same violation of the same desired state".
It is computed ONLY from trusted structural fields:

* the environment target (production / staging),
* the incident class,
* the desired-state reference (a contract item, rule id or invariant name),
* the failure class chosen by the collector,
* the normalised path (UUIDs, numbers, long hex and deployment ids masked).

It deliberately ignores everything that changes between two observations of
the same defect: timestamps, latencies, exact status codes within a class,
request ids, deployment ids and page text (which is never read at all).
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

from .taxonomy import IncidentClass

FINGERPRINT_VERSION = "fp1"

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX = re.compile(r"^[0-9a-fA-F]{8,}$")
_NUM = re.compile(r"^\d+$")
_SAFE = re.compile(r"[^A-Za-z0-9_:./\-]")


def normalize_path(path: Optional[str]) -> str:
    """Mask the parts of a path that identify an instance, not a route."""
    if not path:
        return ""
    p = path.split("?", 1)[0].split("#", 1)[0]
    p = _UUID.sub(":id", p)
    segs = []
    for seg in p.split("/"):
        if _NUM.match(seg) or _HEX.match(seg):
            segs.append(":id")
        else:
            segs.append(seg)
    p = "/".join(segs)
    p = _SAFE.sub("", p)[:160]
    return p.rstrip("/") or "/"


def _token(value: Optional[str], limit: int = 120) -> str:
    return _SAFE.sub("", (value or "").strip())[:limit]


def fingerprint(
    *,
    target: str,
    incident_class: IncidentClass,
    desired_state_ref: str,
    failure: str,
    path: Optional[str] = None,
) -> str:
    """sha256 over the canonical structural tuple, prefixed with the version."""
    parts = (
        FINGERPRINT_VERSION,
        _token(target, 32).lower(),
        incident_class.value,
        _token(desired_state_ref),
        _token(failure, 64).lower(),
        normalize_path(path),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest[:40]}"


def evidence_digest(items: Iterable[str]) -> str:
    """Order-independent digest of structural evidence lines."""
    h = hashlib.sha256()
    for line in sorted(items):
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


__all__ = ["FINGERPRINT_VERSION", "normalize_path", "fingerprint", "evidence_digest"]
