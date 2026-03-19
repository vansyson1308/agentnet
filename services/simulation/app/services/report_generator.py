"""
Report generator — adapted from MiroFish's report_agent.py.

Generates prediction reports from simulation results using LLM.
Falls back to statistical summary if LLM is not configured.

Invariant: Only writes to sim_reports table. Never modifies AgentNet tables.
"""

import logging
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import SimulationConfig
from ..models import SimReport, SimResult, SimSession

logger = logging.getLogger(__name__)


async def generate_report(
    db: Session,
    session: SimSession,
    profiles: List[Dict[str, Any]],
    scenario: Optional[str] = None,
) -> SimReport:
    """
    Generate a prediction report from simulation results.

    Uses LLM if configured, otherwise generates a statistical summary.
    """
    # Fetch simulation results
    results = (
        db.query(SimResult)
        .filter(SimResult.sim_session_id == session.id)
        .order_by(SimResult.step_number, SimResult.agent_index)
        .all()
    )

    # Compute statistics
    stats = _compute_statistics(results, profiles)

    # Generate report content
    if SimulationConfig.is_llm_configured():
        try:
            content, summary, findings = await _generate_llm_report(stats, profiles, scenario, session)
        except Exception as e:
            logger.warning(f"LLM report generation failed: {e}. Using statistical report.")
            content, summary, findings = _generate_statistical_report(stats, profiles, scenario)
    else:
        content, summary, findings = _generate_statistical_report(stats, profiles, scenario)

    # Compute confidence score based on data quality
    confidence = _compute_confidence(stats, len(profiles), session.num_steps)

    report = SimReport(
        id=uuid.uuid4(),
        sim_session_id=session.id,
        report_type="prediction",
        title=f"Simulation Report: {session.name}",
        content=content,
        summary=summary,
        key_findings=findings,
        confidence_score=confidence,
        metadata_={
            "num_steps": session.num_steps,
            "num_agents": len(profiles),
            "platform": session.platform,
            "total_actions": len(results),
        },
    )

    db.add(report)
    db.commit()
    db.refresh(report)

    logger.info(f"Generated report {report.id} for simulation {session.id}")
    return report


def _compute_statistics(
    results: List[SimResult],
    profiles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute aggregate statistics from simulation results."""
    action_counts = Counter()
    agent_activity = defaultdict(int)
    agent_actions = defaultdict(lambda: Counter())
    step_activity = defaultdict(int)

    for r in results:
        if r.action_type and r.action_type != "DO_NOTHING":
            action_counts[r.action_type] += 1
            agent_activity[r.agent_index] += 1
            agent_actions[r.agent_index][r.action_type] += 1
            step_activity[r.step_number] += 1

    # Most active agents
    most_active = sorted(agent_activity.items(), key=lambda x: x[1], reverse=True)[:10]

    # Activity trend (first half vs second half)
    if step_activity:
        max_step = max(step_activity.keys())
        mid = max_step // 2
        first_half = sum(v for k, v in step_activity.items() if k <= mid)
        second_half = sum(v for k, v in step_activity.items() if k > mid)
        trend = (
            "increasing"
            if second_half > first_half * 1.1
            else ("decreasing" if second_half < first_half * 0.9 else "stable")
        )
    else:
        trend = "no_data"

    return {
        "total_actions": sum(action_counts.values()),
        "action_distribution": dict(action_counts),
        "most_active_agents": most_active,
        "agent_actions": {k: dict(v) for k, v in agent_actions.items()},
        "activity_trend": trend,
        "unique_active_agents": len(agent_activity),
        "total_agents": len(profiles),
        "inactivity_rate": 1.0 - (len(agent_activity) / max(len(profiles), 1)),
    }


async def _generate_llm_report(
    stats: Dict[str, Any],
    profiles: List[Dict[str, Any]],
    scenario: Optional[str],
    session: SimSession,
) -> tuple:
    """Generate report using LLM."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=SimulationConfig.LLM_API_KEY,
        base_url=SimulationConfig.LLM_BASE_URL,
    )

    # Build the prompt
    profile_summary = "\n".join(
        f"- {p.get('name', 'Agent')}: {p.get('reputation_tier', 'unranked')} tier, "
        f"capabilities: {p.get('capabilities', [])}"
        for p in profiles[:20]
    )

    prompt = f"""Analyze this multi-agent social simulation and generate a prediction report.

## Simulation Context
- Platform: {session.platform}
- Steps: {session.num_steps}
- Agents: {len(profiles)}
{"- Scenario: " + scenario if scenario else ""}

## Agent Profiles (top 20)
{profile_summary}

## Simulation Statistics
- Total actions: {stats['total_actions']}
- Action distribution: {stats['action_distribution']}
- Activity trend: {stats['activity_trend']}
- Active agents: {stats['unique_active_agents']}/{stats['total_agents']}
- Inactivity rate: {stats['inactivity_rate']:.1%}
- Most active agents: {stats['most_active_agents'][:5]}

## Instructions
Generate a structured prediction report with:
1. Executive Summary (2-3 sentences)
2. Key Findings (3-5 bullet points)
3. Agent Behavior Analysis
4. Market Dynamics Prediction
5. Risk Assessment
6. Recommendations

Format in Markdown. Be specific and data-driven."""

    response = await client.chat.completions.create(
        model=SimulationConfig.LLM_MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": "You are an expert analyst generating predictions from agent-based social simulations.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=SimulationConfig.REPORT_TEMPERATURE,
        max_tokens=3000,
    )

    content = response.choices[0].message.content or ""

    # Extract summary (first paragraph)
    paragraphs = content.split("\n\n")
    summary = paragraphs[0] if paragraphs else content[:200]

    # Build structured findings
    findings = {
        "total_actions": stats["total_actions"],
        "activity_trend": stats["activity_trend"],
        "top_action": (
            max(stats["action_distribution"], key=stats["action_distribution"].get)
            if stats["action_distribution"]
            else None
        ),
        "active_agent_ratio": f"{stats['unique_active_agents']}/{stats['total_agents']}",
        "inactivity_rate": round(stats["inactivity_rate"], 3),
    }

    return content, summary, findings


def _generate_statistical_report(
    stats: Dict[str, Any],
    profiles: List[Dict[str, Any]],
    scenario: Optional[str],
) -> tuple:
    """Generate a statistical report without LLM."""
    action_dist = stats.get("action_distribution", {})
    top_action = max(action_dist, key=action_dist.get) if action_dist else "N/A"

    content = f"""# Simulation Prediction Report

## Executive Summary
The simulation ran {stats.get('total_actions', 0)} total actions across {stats['total_agents']} agents.
Activity trend: **{stats['activity_trend']}**. {stats['unique_active_agents']} agents were active
({1 - stats['inactivity_rate']:.0%} participation rate).

{"## Scenario: " + scenario if scenario else ""}

## Key Findings
- Most common action: **{top_action}** ({action_dist.get(top_action, 0)} occurrences)
- Active agents: {stats['unique_active_agents']}/{stats['total_agents']}
- Activity trend: {stats['activity_trend']}

## Action Distribution
{chr(10).join(f'- {k}: {v}' for k, v in sorted(action_dist.items(), key=lambda x: x[1], reverse=True))}

## Most Active Agents
{chr(10).join(f'- Agent #{idx}: {count} actions' for idx, count in stats['most_active_agents'][:10])}

## Analysis
The simulation reveals a **{stats['activity_trend']}** trend in marketplace activity.
With an inactivity rate of {stats['inactivity_rate']:.1%}, the agent ecosystem shows
{'healthy engagement' if stats['inactivity_rate'] < 0.3 else 'moderate engagement' if stats['inactivity_rate'] < 0.6 else 'low engagement'}.

---
*Generated by AgentNet Simulation Service (statistical mode)*
"""

    summary = (
        f"Simulation completed with {stats['total_actions']} actions, "
        f"{stats['activity_trend']} trend, "
        f"{stats['unique_active_agents']}/{stats['total_agents']} active agents."
    )

    findings = {
        "total_actions": stats["total_actions"],
        "activity_trend": stats["activity_trend"],
        "top_action": top_action,
        "active_agent_ratio": f"{stats['unique_active_agents']}/{stats['total_agents']}",
        "inactivity_rate": round(stats["inactivity_rate"], 3),
    }

    return content, summary, findings


def _compute_confidence(stats: Dict[str, Any], num_agents: int, num_steps: int) -> float:
    """
    Compute confidence score (0.0 - 1.0) based on data quality.

    Higher confidence with more agents, more steps, and higher participation.
    """
    agent_factor = min(1.0, num_agents / 30)  # Max confidence at 30+ agents
    step_factor = min(1.0, num_steps / 50)  # Max confidence at 50+ steps
    participation = 1.0 - stats.get("inactivity_rate", 0.5)

    confidence = agent_factor * 0.3 + step_factor * 0.3 + participation * 0.4
    return round(min(1.0, max(0.0, confidence)), 3)
