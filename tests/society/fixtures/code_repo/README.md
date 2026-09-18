# Society code fixture repository

A deliberately isolated, harmless application module with ONE planted defect
(`parse_bool` rejects `yes`/`on`). `tests/society/conftest.py::make_code_repo`
copies it into a throw-away git repository so the deterministic
self-development proof can investigate, fix, test, review, promote (shadow)
and evaluate a REAL source-code change without touching this checkout.

The fixture is data for tests. It is never imported by the services.
