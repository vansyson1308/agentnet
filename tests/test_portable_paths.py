"""Every tracked path can be checked out on Windows (scripts/ci/check_portable_paths.py)."""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_portable_paths", ROOT / "scripts" / "ci" / "check_portable_paths.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_each_windows_forbidden_character_is_caught():
    bad = [f"docs/a{c}b.md" for c in ':"<>|?*']
    ok = ["services/registry/app/features/dashboard_gửi_tin_nhắn_trực_tiếp_từ_ui.md", "docs/search_&_filter (v2).md", "a/b'c.md"]
    assert check.offending(bad + ok) == bad


def test_the_repository_has_no_windows_hostile_path():
    assert check.offending(check.tracked_paths()) == []
