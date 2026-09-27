"""Maintenance release attestations (ADR-0010 D15).

An attestation is an immutable, canonical JSON statement of the facts a
GREEN (or owner-approved AMBER) repair was released on. Every field comes
from durable rows written by deterministic code -- none from model text.

It is hashed (sha256 over canonical JSON) and signed with HMAC-SHA256 when
``MAINTENANCE_ATTESTATION_KEY`` is set. The signature makes tampering with
the stored row detectable; it is NOT what makes a release safe: the Release
Controller re-derives every security-relevant field itself (diff, protected
paths, risk, CI, freeze, budget) from trusted code and provider APIs, and
refuses on any mismatch (fail closed).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Dict, Tuple

ATTESTATION_VERSION = "maint-attest/1"
REQUIRED_FIELDS = (
    "version",
    "incident_id",
    "repair_case_id",
    "base_sha",
    "head_sha",
    "merged_sha",
    "tree_sha",
    "diff_digest",
    "changed_files",
    "risk_decision",
    "ci",
    "qa_verdict",
    "security_verdict",
    "staging_proof",
    "browser_proof",
    "contract_proof",
    "slo_state",
    "known_good_production_sha",
    "owner_approval",
    "created_at",
)


def canonical_bytes(att: Dict[str, Any]) -> bytes:
    return json.dumps(att, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False).encode("utf-8")


def _key() -> bytes:
    return (os.getenv("MAINTENANCE_ATTESTATION_KEY") or "").encode("utf-8")


def sign(att: Dict[str, Any]) -> Tuple[str, str]:
    """(digest, signature). ``signature`` is ``unsigned`` without a key --
    the release controller refuses unsigned attestations outside tests."""
    missing = [f for f in REQUIRED_FIELDS if f not in att]
    if missing:
        raise ValueError(f"attestation missing fields: {missing}")
    body = canonical_bytes(att)
    d = hashlib.sha256(body).hexdigest()
    key = _key()
    sig = hmac.new(key, body, hashlib.sha256).hexdigest() if key else "unsigned"
    return d, sig


def verify(att: Dict[str, Any], digest: str, signature: str, *, require_signature: bool) -> Tuple[bool, str]:
    missing = [f for f in REQUIRED_FIELDS if f not in att]
    if missing:
        return False, f"missing fields: {missing}"
    if att.get("version") != ATTESTATION_VERSION:
        return False, "unknown attestation version"
    body = canonical_bytes(att)
    if hashlib.sha256(body).hexdigest() != digest:
        return False, "digest mismatch (attestation was modified)"
    key = _key()
    if signature == "unsigned":
        return (not require_signature), ("unsigned attestation" if require_signature else "ok (unsigned; signature not required)")
    if not key:
        return False, "signed attestation but no verification key configured"
    if not hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest(), signature):
        return False, "signature mismatch"
    return True, "ok"


__all__ = ["ATTESTATION_VERSION", "REQUIRED_FIELDS", "sign", "verify", "canonical_bytes"]
