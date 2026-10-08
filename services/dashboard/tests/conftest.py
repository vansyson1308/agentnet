"""Test isolation for the dashboard gates.

``deploy.maintenance.dashboard_fixture.serve_dashboard`` (used by
test_experience_contract.py) patches fixture data onto the shared
``main.api_client`` INSTANCE and never removes it. Those instance attributes
then shadow the class-level stub of test_public_surface.py when both files
run in one pytest process. Restore the instance after every module; no
judge or assertion changes.
"""

import pytest


@pytest.fixture(autouse=True, scope="module")
def _isolate_dashboard_api_client():
    try:
        from services.dashboard.app import main
    except ImportError:  # run from services/dashboard (test_main.py): nothing shared to restore
        yield
        return
    before = dict(vars(main.api_client))
    yield
    vars(main.api_client).clear()
    vars(main.api_client).update(before)
