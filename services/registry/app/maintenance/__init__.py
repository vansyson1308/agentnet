"""AgentNet Autonomous Maintenance OS (ADR-0010).

A deterministic control plane that turns trusted observations of the product
into durable Maintenance Incidents and Repair Cases, drives every case to a
terminal outcome by reconciliation, and releases verified GREEN repairs to
production through a separate, model-free Release Controller.

Agents (the model) are cognitive workers here: they diagnose, design and
author patches through typed, bounded activities. They never own workflow
state, retries, deadlines, risk, release eligibility or credentials.

This package is TRUSTED BASE code (risk.py classifies it RED): a repair
candidate that edits it is never auto-released.
"""
