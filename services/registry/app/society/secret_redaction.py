"""Key-shaped strings never reach a persisted intent row (staging audit X01, 2026-10-27).

A model writes free text: a design, a ticket, an SDK example. When that text holds a
string shaped like a provider key (``sk-…``/``dsk-…``) or a JWT, it is replaced by a
fixed marker before the ``agent_intents`` row is written. What survives is structural
only: the field it was in, the pattern class and whether its fingerprint matches a
configured credential or the compromised list (``canary.COMPROMISED_CREDENTIAL_FINGERPRINTS``).
The matched text itself is never logged, returned or stored.

A match against a real credential is a leak, not a placeholder: the worker logs it at
ERROR so the operator rotates the credential (the value is still redacted)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

from .canary import COMPROMISED_CREDENTIAL_FINGERPRINTS, fingerprint

logger = logging.getLogger(__name__)

# Same shapes as the audit (deploy/railway/phase5_live.py KEY_SHAPE/JWT_SHAPE); the
# look-behind keeps ids such as "task-…" from matching "sk-".
KEY_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?:sk|dsk)-[A-Za-z0-9_\-]{16,}")
JWT_SHAPE = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
SHAPES = (("key", KEY_SHAPE), ("jwt", JWT_SHAPE))
MARKER = "[redacted-{cls}]"


@dataclass(frozen=True)
class Finding:
    field: str  # dotted path inside the row ("payload.description", "payload.files[0]")
    cls: str  # "key" | "jwt"
    credential_match: bool  # the fingerprint is a configured credential or on the compromised list

    def to_dict(self) -> dict:
        return {"field": self.field, "class": self.cls, "credential_match": self.credential_match}


def _known(credentials: Iterable[str]) -> frozenset:
    return frozenset(fingerprint(c) for c in credentials if c) | COMPROMISED_CREDENTIAL_FINGERPRINTS


def redact_text(text: str, field: str, known: frozenset) -> Tuple[str, List[Finding]]:
    found: List[Finding] = []
    for cls, shape in SHAPES:
        def sub(m: "re.Match[str]", cls: str = cls) -> str:
            found.append(Finding(field, cls, fingerprint(m.group(0)) in known))
            return MARKER.format(cls=cls)

        text = shape.sub(sub, text)
    return text, found


def redact(value: Any, field: str = "", *, credentials: Iterable[str] = (), _known_fps: Optional[frozenset] = None) -> Tuple[Any, List[Finding]]:
    """``value`` with every key-/JWT-shaped substring replaced, and where each one was."""
    known = _known(credentials) if _known_fps is None else _known_fps
    if isinstance(value, str):
        return redact_text(value, field, known)
    if isinstance(value, dict):
        out, found = {}, []
        for k, v in value.items():
            out[k], f = redact(v, f"{field}.{k}" if field else str(k), _known_fps=known)
            found += f
        return out, found
    if isinstance(value, (list, tuple)):
        items, found = [], []
        for i, v in enumerate(value):
            item, f = redact(v, f"{field}[{i}]", _known_fps=known)
            items.append(item)
            found += f
        return items, found
    return value, []


INTENT_FIELDS = ("payload", "policy_reason", "result", "error")


def scrub_intent(row: Any, *, credentials: Iterable[str] = (), role: str = "") -> List[Finding]:
    """Redact an ``AgentIntent`` row in place before it is written; idempotent (the marker
    never matches a shape). Logs one structural line per finding, at ERROR when it matches
    a real credential -- never the matched text."""
    known = _known(credentials)
    found: List[Finding] = []
    for name in INTENT_FIELDS:
        value = getattr(row, name, None)
        if value is None:
            continue
        clean, f = redact(value, name, _known_fps=known)
        if f:
            setattr(row, name, clean)
            found += f
    for f in found:
        log = logger.error if f.credential_match else logger.warning
        log("secret.redacted intent=%s type=%s role=%s field=%s class=%s credential_match=%s%s", str(getattr(row, "id", ""))[:8], getattr(row, "intent_type", ""),
            role, f.field, f.cls, f.credential_match, " ROTATE THIS CREDENTIAL" if f.credential_match else "")
    return found
