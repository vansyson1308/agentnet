"""Content-addressed artifact storage with atomic finalization."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .config import ARTIFACT_STORE_PATH


class ArtifactIntegrityError(RuntimeError):
    pass


def store_bytes(content: bytes, expected_sha256: str, expected_size: int) -> str:
    if len(content) != expected_size:
        raise ArtifactIntegrityError("artifact size does not match declared size")
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise ArtifactIntegrityError("artifact SHA-256 does not match declared hash")

    root = Path(ARTIFACT_STORE_PATH)
    destination = root / "sha256" / actual[:2] / actual
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{actual}.", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return f"sha256/{actual[:2]}/{actual}"
