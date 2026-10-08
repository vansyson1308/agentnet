"""Fingerprints, trusted maintenance risk, PatchSet atomicity, paged repo reads."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from services.registry.app.maintenance import policy as pol
from services.registry.app.maintenance.fingerprint import fingerprint, normalize_path
from services.registry.app.maintenance.patchset import PatchError, PatchSet, apply_patchset
from services.registry.app.maintenance.repo_tools import RepoTools, RepoToolError, page_text
from services.registry.app.maintenance.taxonomy import IncidentClass, MaintenanceRiskClass as MRC

from .conftest import MAIN, PAGE, VERIFY

# ── fingerprints ──────────────────────────────────────────────────────────


def fp(**over):
    base = dict(target="production", incident_class=IncidentClass.UI_RENDERING, desired_state_ref="marketplace", failure="raw_structured_value", path="/marketplace")
    base.update(over)
    return fingerprint(**base)


def test_fingerprint_is_stable_across_ids_timestamps_and_deployments():
    assert normalize_path("/network/agents/11111111-1111-4111-8111-111111111111") == "/network/agents/:id"
    assert normalize_path("/tasks/1234/trace?ts=1727450000#x") == "/tasks/:id/trace"
    assert normalize_path("/deploy/0a1b2c3d4e5f6a7b") == "/deploy/:id"
    assert fp(path="/network/agents/11111111-1111-4111-8111-111111111111") == fp(path="/network/agents/22222222-2222-4222-8222-222222222222")
    assert fp(path="/tasks/1/trace") == fp(path="/tasks/99/trace")


def test_fingerprint_distinguishes_different_failures():
    fps = {fp(), fp(failure="text_contrast"), fp(desired_state_ref="login"), fp(target="staging"), fp(incident_class=IncidentClass.ACCESSIBILITY), fp(path="/network")}
    assert len(fps) == 6


# ── trusted maintenance risk ────────────────────────────────────────────────


def cls(paths, diff=""):
    return pol.classify_patch(paths, diff).risk_class


def test_template_and_static_presentation_fixes_can_be_maintenance_green():
    assert cls([PAGE], f"+++ b/{PAGE}\n+<li>echo</li>\n-<li>{{'a': 1}}</li>\n") is MRC.MAINTENANCE_GREEN
    assert cls(["services/dashboard/app/static/css/dark.css"], "+++ b/services/dashboard/app/static/css/dark.css\n+body{color:#e6e8eb}\n") is MRC.MAINTENANCE_GREEN


def test_route_code_is_amber_and_sensitive_surfaces_are_red_or_refused():
    assert cls([MAIN]) is MRC.AMBER
    assert cls(["services/payment/app/wallet.py"]) is MRC.RED
    assert cls(["services/registry/app/task_service.py"]) is MRC.RED
    assert cls(["services/registry/app/maintenance/release.py"]) is MRC.RED
    assert cls(["docs/adr/0010-autonomous-maintenance-os.md"]) is MRC.RED
    assert cls([".railway/production.ts"]) is MRC.RED
    assert cls([".env.production"]) is MRC.CONSTITUTIONAL


def test_evaluation_laundering_is_red():
    d = pol.classify_patch([PAGE, "services/dashboard/tests/test_public_surface.py"], "")
    assert d.risk_class is MRC.RED and any("laundering" in r for r in d.reasons)
    assert cls([PAGE, "services/registry/app/society/public_surface_contract.json"]) is MRC.RED


@pytest.mark.parametrize("added,finding", [
    ("    except Exception:\n+        pass", "catch_all_exception_added"),
    ("app.url_build_error_handlers.append(x)", "url_build_fallback_added"),
    ("    return '#'", "placeholder_link_fallback_added"),
    ("@app.errorhandler(404)", "error_handler_rewritten"),
    (".alert { display: none }", "error_banner_hidden"),
])
def test_anti_reward_hacking_findings_refuse_the_patch(added, finding):
    lines = "\n".join("+" + ln.lstrip("+") for ln in added.split("\n"))
    d = pol.classify_patch([PAGE], f"--- a/{PAGE}\n+++ b/{PAGE}\n{lines}\n")
    assert d.risk_class is MRC.CONSTITUTIONAL and any(finding in f for f in d.findings), d.to_dict()


def test_removing_the_error_message_is_reward_hacking():
    d = pol.classify_patch([MAIN], f"--- a/{MAIN}\n+++ b/{MAIN}\n-    flash(\"Page not found.\", \"warning\")\n")
    assert d.risk_class is MRC.CONSTITUTIONAL


def test_money_and_auth_semantics_in_the_diff_raise_the_class():
    assert cls([PAGE], f"+++ b/{PAGE}\n+{{{{ wallet.balance_credits }}}}\n") is MRC.RED
    assert cls([PAGE], f"+++ b/{PAGE}\n+<a href=\"/logout\">{{{{ session['access_token'] }}}}</a>\n") is MRC.AMBER


def test_oversized_diffs_and_scopes_leave_green():
    big = f"+++ b/{PAGE}\n" + "\n".join(f"+<p>{i}</p>" for i in range(500))
    assert cls([PAGE], big) is MRC.AMBER
    many = [f"services/dashboard/app/templates/p{i}.html" for i in range(12)]
    assert cls(many) is MRC.AMBER


def test_rescope_crossing_a_boundary_is_detected():
    assert pol.escalates("MAINTENANCE_GREEN", MRC.AMBER) and pol.escalates("AMBER", MRC.RED)
    assert not pol.escalates("RED", MRC.AMBER) and not pol.escalates(None, MRC.RED)


# ── PatchSet ─────────────────────────────────────────────────────────────


@pytest.fixture
def ws(product_repo, tmp_path, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache
    from services.registry.app.maintenance.harness import open_attempt_workspace

    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(product_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "ws"))
    reset_settings_cache()
    yield open_attempt_workspace(SocietySettings(), "00000000-0000-4000-8000-000000000001", 1)
    reset_settings_cache()


def patch(ws, files, **kw):
    return PatchSet.model_validate({"base_sha": ws.base_sha, "files": files, **kw})


def test_multi_file_patch_applies_atomically(ws):
    p = patch(ws, [
        {"path": PAGE, "operations": [{"op": "replace_exact", "old": "{'name': 'echo', 'price': 1}", "new": "echo"}, {"op": "insert_after", "anchor": "<h1>AgentNet Marketplace</h1>", "text": "\n<p>agents</p>"}]},
        {"path": MAIN, "operations": [{"op": "insert_before", "anchor": "def marketplace", "text": "# route\n"}]},
    ])
    out = apply_patchset(ws, p, files_allowed=[PAGE, MAIN])
    assert sorted(out.written) == sorted([PAGE, MAIN])
    text = (ws.path / PAGE).read_text()
    assert "echo</li>" in text and "<p>agents</p>" in text


@pytest.mark.parametrize("files,code", [
    ([{"path": PAGE, "operations": [{"op": "replace_exact", "old": "li", "new": "x"}]}], "ambiguous"),
    ([{"path": PAGE, "operations": [{"op": "replace_exact", "old": "does-not-exist", "new": "x"}]}], "no_match"),
    ([{"path": VERIFY, "operations": [{"op": "replace_exact", "old": "assert", "new": "pass #"}]}], "out_of_scope"),
    ([{"path": PAGE, "operations": [{"op": "delete"}]}], "protected"),
    ([{"path": "services/dashboard/app/templates/new.html", "operations": [{"op": "create", "text": "x"}]}, {"path": PAGE, "operations": [{"op": "replace_exact", "old": "nope", "new": "y"}]}], "no_match"),
])
def test_invalid_patches_write_nothing(ws, files, code):
    before = (ws.path / PAGE).read_text()
    allowed = [PAGE, MAIN, "services/dashboard/app/templates/new.html"]
    with pytest.raises(PatchError) as ei:
        apply_patchset(ws, patch(ws, files), files_allowed=allowed)
    assert ei.value.code == code
    assert (ws.path / PAGE).read_text() == before
    assert not (ws.path / "services/dashboard/app/templates/new.html").exists(), "all-or-nothing: the create before the failure was not written"


def test_stale_base_and_protected_paths_fail_before_modification(ws):
    with pytest.raises(PatchError) as ei:
        apply_patchset(ws, PatchSet.model_validate({"base_sha": "deadbeefdead", "files": [{"path": PAGE, "operations": [{"op": "create", "text": "x"}]}]}), files_allowed=[PAGE])
    assert ei.value.code == "stale_base"
    with pytest.raises(PatchError) as ei:
        apply_patchset(ws, patch(ws, [{"path": ".env", "operations": [{"op": "create", "text": "SECRET=1"}]}]), files_allowed=[".env"])
    assert ei.value.code == "protected"
    with pytest.raises(ValueError):
        patch(ws, [{"path": "../etc/passwd", "operations": [{"op": "create", "text": "x"}]}])


def test_a_created_file_may_be_deleted_but_never_a_base_file(ws):
    new = "services/dashboard/app/templates/tmp.html"
    apply_patchset(ws, patch(ws, [{"path": new, "operations": [{"op": "create", "text": "x"}]}]), files_allowed=[new])
    apply_patchset(ws, patch(ws, [{"path": new, "operations": [{"op": "delete"}]}]), files_allowed=[new])
    assert not (ws.path / new).exists()


# ── paged repository intelligence ─────────────────────────────────────────


def test_paging_is_explicit_and_whole_lines():
    text = "".join(f"line {i} " + "x" * 50 + "\n" for i in range(1, 501))
    p = page_text("f.py", text, start_line=1, max_bytes=2000)
    assert p.truncated and p.next_line == p.end_line + 1 and p.total_lines == 500
    assert p.content.endswith("\n") and p.content.count("\n") == p.end_line
    last = page_text("f.py", text, start_line=490, max_bytes=100000)
    assert not last.truncated and last.next_line is None and last.end_line == 500


def test_repo_tools_on_the_real_dashboard_map_routes_and_template_refs():
    # The routing defect (f33f067 deleted /login, /register, /marketplace) is
    # repaired; the tools must see those routes and no dangling reference.
    repo = RepoTools(pathlib.Path(__file__).resolve().parents[3])
    refs = repo.template_refs()
    routes = {r["endpoint"] for r in repo.route_map()["routes"]}
    assert {"login_page", "register_page", "marketplace_page"} <= routes
    assert "login_page" not in refs["unregistered_endpoints"]
    assert repo.find_symbol("derive_trust_context")["definitions"][0]["path"] == "services/dashboard/app/main.py"
    page = repo.read_range("services/dashboard/app/main.py", 1)
    assert page["total_lines"] > 0 and "truncated" in page
    with pytest.raises(RepoToolError):
        repo.read_range(".env")
    with pytest.raises(RepoToolError):
        repo.read_range("../../etc/passwd")


def test_test_ownership_finds_the_verification_test(product_repo):
    subprocess.run(["git", "status"], cwd=product_repo, capture_output=True)
    repo = RepoTools(product_repo)
    assert repo.list_definitions(MAIN)["definitions"][0]["name"] == "marketplace"
