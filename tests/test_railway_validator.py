"""Pure helpers of the in-Railway staging validator (deploy/railway/validate_staging.py).

The validator itself runs only inside the staging environment; these tests
pin the parts that decide PASS/FAIL and the never-print-a-secret contract."""

from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
PATH = REPO / "deploy/railway/validate_staging.py"


def _load():
    spec = importlib.util.spec_from_file_location("validate_staging", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_derived_password_satisfies_the_registry_policy():
    v = _load()
    for secret in ("a" * 64, "0123456789abcdef" * 4, "", "ZZ-99"):
        pw = v.derive_password(secret)
        assert len(pw) >= 12 and any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
    assert v.derive_password("a" * 64) != v.derive_password("b" * 64)


def test_spoof_analysis_passes_only_when_forged_headers_share_the_bucket():
    v = _load()
    ok, _ = v.analyse_spoof([401] * 150 + [429] * 70, [429] * 60)
    assert ok
    ok, _ = v.analyse_spoof([401] * 150 + [429] * 70, [401] * 10 + [429] * 50)
    assert ok, "429 within the forged run and no later than the baseline"
    ok, note = v.analyse_spoof([401] * 150 + [429] * 70, [401] * 60)
    assert not ok and "fresh bucket" in note
    ok, note = v.analyse_spoof([401] * 220, [429] * 60)
    assert not ok and "never tripped" in note
    ok, _ = v.analyse_spoof([401] * 100 + [429] * 120, [401] * 120 + [429] * 5)
    assert not ok, "forged run tripped later than the baseline"


def test_child_output_summary_keeps_only_verdict_lines():
    v = _load()
    text = "Authorization: Bearer eyJ.secret\nPASS C01 healthz\nFAIL C05 leak — note\nSKIP C12 probe\ngarbage\nSOCIETY SMOKE: PASS\n"
    lines = v.summarise_script_output(text)
    assert lines == ["PASS C01 healthz", "FAIL C05 leak — note", "SKIP C12 probe", "SOCIETY SMOKE: PASS"]
    assert not any("eyJ" in ln for ln in lines)


def test_validator_never_prints_tokens_or_passwords():
    text = PATH.read_text(encoding="utf-8")
    # every stdout write is a CHECK/VALIDATOR/summary line; tokens and the password are never interpolated
    for m in re.finditer(r"sys\.stdout\.write\((.+)\)", text):
        arg = m.group(1)
        assert "token" not in arg.lower() and "password" not in arg.lower() and "secret\"" not in arg, arg
    assert "--expect-runtime\", \"off\"" in text, "the society runtime is asserted OFF"
    assert "auth/user/login" in text and "auth/user/register" in text and "/v1/agents/public/" in text
