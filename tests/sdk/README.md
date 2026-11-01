# tests/sdk

Tests that judge `sdk/` live here, next to what they judge
(`society/spec_gate.py` PLACEMENT). Required CI collects all of `tests/`; a test placed in
`tests/society/` would make the change RED (owner merge) although it touches no society code.
Test file names must be unique across `tests/` (no `__init__.py` here, like `tests/dashboard/`).
