#!/usr/bin/env python3
"""Run one cycle of PlannerV5 and exit."""
import sys, os
sys.path.insert(0, "/opt/agentnet")

# Force unbuffered
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

os.environ["PYTHONUNBUFFERED"] = "1"

from hermes_planner import PlannerV5

planner = PlannerV5()
planner.log.info("=== RUNNING ONE CYCLE ===")

# Run just once: process QA results, completed, enrich, dispatch
planner._process_qa_results()
planner._process_completed()
if not planner._enrich_minimal_open():
    planner._dispatch_ready()

planner.log.info("=== DONE ===")
