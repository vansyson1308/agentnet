"""The registry image carries the dashboard's runtime so the Society's QA and
fitness can exercise dashboard candidates (the society-worker runs the
registry image). One version everywhere: the copies must equal the
dashboard's own pins, and the registry application must never import them."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pins(path: Path) -> dict:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*==\s*([^\s#]+)", line)
        if m:
            pins[m.group(1).split("[")[0].lower()] = m.group(2)
    return pins


def test_registry_pins_the_dashboard_runtime_at_the_dashboard_versions():
    dash = _pins(ROOT / "services" / "dashboard" / "requirements.txt")
    reg = _pins(ROOT / "services" / "registry" / "requirements.txt")
    for name in ("flask", "requests"):
        assert name in dash and reg.get(name) == dash[name], f"{name}: registry {reg.get(name)} != dashboard {dash.get(name)}"


def test_the_registry_application_never_imports_the_dashboard_runtime():
    app = ROOT / "services" / "registry" / "app"
    offenders = [
        str(p.relative_to(ROOT))
        for p in app.rglob("*.py")
        if re.search(r"^\s*(from\s+flask\b|import\s+flask\b)", p.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert not offenders, offenders
