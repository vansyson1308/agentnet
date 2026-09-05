"""
Runtime contract: Python version and password hashing (Phase 2.6 §9).

``passlib`` 1.7.4 (unmaintained upstream) imports the stdlib ``crypt`` module
at import time; Python 3.11 deprecates it and 3.13 removes it. The import is
guarded (``except ImportError``) and bcrypt hashing never uses it, so the only
real risk is an interpreter jump that nobody decided on. The service images
declare their interpreter explicitly; CI runs the suite on 3.11 and the
isolated per-service check on the images' 3.10. These tests pin that contract
and prove bcrypt hashing keeps working with ``crypt`` absent.

``bcrypt`` is held at 4.0.0 everywhere passlib is used: passlib 1.7.4 reads
``bcrypt.__about__.__version__`` (gone in 4.1) and bcrypt 5 changed the >72-byte
password behaviour it probes with — either breaks backend detection.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SUPPORTED_IMAGE_PYTHONS = {"3.10", "3.11"}


@pytest.mark.parametrize("svc", ["registry", "payment", "worker", "simulation", "dashboard"])
def test_service_images_pin_a_supported_python(svc):
    text = (REPO / "services" / svc / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^FROM python:(\d+\.\d+)-slim", text, re.M)
    assert m, f"{svc}: FROM python:<major.minor>-slim expected"
    assert m.group(1) in SUPPORTED_IMAGE_PYTHONS, f"{svc} moved to Python {m.group(1)}: review passlib/crypt first (ADR-0003 D8)"


def test_ci_interpreters_match_the_contract():
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r'PYTHON_VERSION:\s*"3\.11"', ci)
    assert re.search(r'python-version:\s*"3\.10"', ci), "per-service isolation job must run on the images' Python"


@pytest.mark.parametrize("svc", ["registry", "payment", "simulation"])
def test_bcrypt_is_pinned_wherever_passlib_is(svc):
    req = (REPO / "services" / svc / "requirements.txt").read_text(encoding="utf-8")
    assert "passlib[bcrypt]==1.7.4" in req
    assert re.search(r"^bcrypt==4\.0\.0$", req, re.M), f"{svc}: bcrypt must be pinned to 4.0.0 next to passlib 1.7.4"


def test_bcrypt_hashing_survives_without_stdlib_crypt():
    """Simulate Python 3.13 (no ``crypt`` module): passlib must import and
    bcrypt hash/verify must work, and the existing hash format stays valid."""
    code = (
        "import sys; sys.modules['crypt'] = None\n"
        "from passlib.context import CryptContext\n"
        "ctx = CryptContext(schemes=['bcrypt'], deprecated='auto')\n"
        "h = ctx.hash('correct horse')\n"
        "assert h.startswith('$2b$'), h\n"
        "assert ctx.verify('correct horse', h) and not ctx.verify('wrong', h)\n"
        "import passlib.utils as u; assert u.has_crypt is False\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
