"""A "<src>:placeholders" incident recovers once the page is clean.

Live 2026-10-08 (production, after PR #70): surface.py emitted a
"<src>:placeholders" observation only when a page HAD dead links, so the
landing/metaverse/network placeholder incidents never received a healthy
sample and stayed open with healthy_streak=0 although production had no '#'
link left. A clean crawled page now emits an ok observation of the same name.
"""

from __future__ import annotations

import importlib.util
import pathlib

from services.registry.app.maintenance.orm import MaintenanceIncident
from services.registry.app.maintenance.surface_ingest import ingest_report
from services.registry.app.maintenance.taxonomy import IncidentStatus
from services.registry.app.society import surface

from .conftest import at

_FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "test_public_surface_contract.py"
_spec = importlib.util.spec_from_file_location("surface_contract_site_fixture", _FIXTURE)
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


def _probe(dirty: bool) -> surface.SurfaceReport:
    routes = site.healthy_routes()
    if dirty:
        routes[("ui", "/landing")] = (200, {}, site._page("Welcome", '<a href="#">Sign In</a><a href="/marketplace">Explore Marketplace</a>'))
    with site.client_for(routes) as client:
        return surface.run_contract(site.ORIGINS, client=client, only_monitored=True)


def test_a_clean_page_emits_an_ok_placeholder_observation_for_every_navigation_source():
    obs = {o.name: o for o in _probe(dirty=False).observations if o.kind == "placeholder"}
    assert set(obs) == {f"{src}:placeholders" for src in surface.load_contract().navigation.sources}
    assert all(o.ok and o.detail == "" for o in obs.values())
    dirty = {o.name: o for o in _probe(dirty=True).observations if o.kind == "placeholder"}
    assert not dirty["landing:placeholders"].ok and dirty["landing:placeholders"].failure == surface.PLACEHOLDER_LINK
    assert all(o.ok for n, o in dirty.items() if n != "landing:placeholders")


def test_an_open_placeholder_incident_recovers_after_three_clean_probes(db, mset):
    def incident():
        return db.query(MaintenanceIncident).filter(MaintenanceIncident.desired_state_ref == "landing:placeholders").one()

    for minute in (0, 1):
        ingest_report(db, mset, _probe(dirty=True), now=at(minute))
    db.commit()
    assert incident().status == IncidentStatus.OPEN.value and incident().healthy_streak == 0

    for i, minute in enumerate((10, 11, 12), start=1):
        out = ingest_report(db, mset, _probe(dirty=False), now=at(minute))
        db.commit()
        assert incident().healthy_streak == i
        if i < mset.recovery_streak:
            assert incident().status == IncidentStatus.OPEN.value and not out["recovered"]
    inc = incident()
    assert inc.status == IncidentStatus.RECOVERED.value and out["recovered"] == [str(inc.id)]
