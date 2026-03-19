"""
Knowledge graph builder — adapted from MiroFish's graph_builder.py.

Builds a knowledge graph from AgentNet seed data using Zep Cloud.
If Zep is not configured, falls back to a simple in-memory graph
representation using dicts.

Invariant: Read-only operation. Does not modify any AgentNet tables.
"""

import logging
from typing import Any, Dict, List

from ..config import SimulationConfig

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """In-memory knowledge graph representation."""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []

    def add_node(self, node_id: str, node_type: str, data: Dict[str, Any]):
        self.nodes[node_id] = {"type": node_type, **data}

    def add_edge(self, from_id: str, to_id: str, relation: str, weight: float = 1.0):
        self.edges.append(
            {
                "from": from_id,
                "to": to_id,
                "relation": relation,
                "weight": weight,
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "num_nodes": len(self.nodes),
            "num_edges": len(self.edges),
        }


async def build_knowledge_graph(
    seed_data: Dict[str, Any],
    project_id: str,
) -> KnowledgeGraph:
    """
    Build a knowledge graph from extracted seed data.

    If Zep is configured, uses Zep Cloud for persistent graph storage.
    Otherwise, builds an in-memory graph.
    """
    graph = KnowledgeGraph()

    agents = seed_data.get("agents", [])
    interactions = seed_data.get("interactions", [])
    task_history = seed_data.get("task_history", [])

    # Add agent nodes
    for agent in agents:
        graph.add_node(
            node_id=agent["id"],
            node_type="agent",
            data={
                "name": agent.get("name", "unknown"),
                "description": agent.get("description", ""),
                "capabilities": agent.get("capabilities", []),
                "reputation_tier": agent.get("reputation_tier", "unranked"),
                "success_rate": agent.get("success_rate", 0),
            },
        )

    # Add interaction edges
    for interaction in interactions:
        graph.add_edge(
            from_id=interaction["from_agent_id"],
            to_id=interaction["to_agent_id"],
            relation=interaction.get("interaction_type", "unknown"),
            weight=float(interaction.get("count", 1)),
        )

    # Add task pattern edges
    for task in task_history:
        agent_id = task.get("callee_agent_id")
        if agent_id and agent_id in graph.nodes:
            # Enrich agent node with task patterns
            node = graph.nodes[agent_id]
            if "task_patterns" not in node:
                node["task_patterns"] = []
            node["task_patterns"].append(
                {
                    "status": task.get("status"),
                    "count": task.get("task_count", 0),
                    "avg_amount": task.get("avg_amount", 0),
                    "avg_duration": task.get("avg_duration_secs", 0),
                }
            )

    # If Zep is configured, persist the graph to Zep Cloud
    if SimulationConfig.is_zep_configured():
        try:
            await _persist_to_zep(graph, project_id)
        except Exception as e:
            logger.warning(f"Failed to persist graph to Zep: {e}. Using in-memory graph.")

    logger.info(f"Built knowledge graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")
    return graph


async def _persist_to_zep(graph: KnowledgeGraph, project_id: str):
    """Persist graph to Zep Cloud for long-term memory."""
    try:
        from zep_python import ZepClient

        client = ZepClient(api_key=SimulationConfig.ZEP_API_KEY)

        # Build a text summary for Zep to ingest
        summary_parts = []
        for node_id, node_data in graph.nodes.items():
            if node_data.get("type") == "agent":
                summary_parts.append(
                    f"Agent '{node_data.get('name', 'unknown')}' (ID: {node_id}) "
                    f"has reputation tier '{node_data.get('reputation_tier', 'unranked')}' "
                    f"with success rate {node_data.get('success_rate', 0):.1%}. "
                    f"Capabilities: {node_data.get('capabilities', [])}."
                )

        for edge in graph.edges:
            summary_parts.append(
                f"Agent {edge['from']} has relationship '{edge['relation']}' "
                f"with Agent {edge['to']} (strength: {edge['weight']})."
            )

        text = "\n".join(summary_parts)

        # Add to Zep memory
        await client.memory.add(
            session_id=f"sim_{project_id}",
            messages=[{"role": "system", "content": text}],
        )

        logger.info(f"Persisted graph to Zep session sim_{project_id}")

    except ImportError:
        logger.warning("zep-python not installed. Skipping Zep persistence.")
    except Exception as e:
        logger.warning(f"Zep persistence error: {e}")
