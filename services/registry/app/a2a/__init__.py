"""A2A v1 integration for AgentNet (ADR-0009).

Deliberately import-light: ``app/models.py`` imports ``app.a2a.orm`` at load
time, so nothing here may import the handler, routes or federation modules.
"""
