"""
Tests for simulation seed data extraction.

Tests:
1. Seed extractor functions exist and are callable
2. Graph builder produces valid structures
3. Profile generator creates OASIS-compatible profiles

Note: These tests verify logic without requiring a database connection.
Database-dependent integration tests should use fixtures.
"""


class TestSeedExtractorModule:
    """Verify seed extractor module structure."""

    def test_extract_functions_exist(self):
        """All expected extraction functions should be importable."""
        from services.simulation.app.services.seed_extractor import (
            extract_full_seed,
            extract_interactions,
            extract_seed_agents,
            extract_task_history,
        )

        assert callable(extract_seed_agents)
        assert callable(extract_interactions)
        assert callable(extract_task_history)
        assert callable(extract_full_seed)


class TestGraphBuilder:
    """Test knowledge graph builder logic."""

    def test_knowledge_graph_class(self):
        """KnowledgeGraph should support nodes and edges."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph

        graph = KnowledgeGraph()
        assert hasattr(graph, "nodes")
        assert hasattr(graph, "edges")
        assert isinstance(graph.nodes, dict)
        assert isinstance(graph.edges, list)

    def test_graph_add_node(self):
        """Should be able to add nodes to the graph."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.nodes["agent-1"] = {
            "id": "agent-1",
            "name": "TestAgent",
            "type": "agent",
        }
        assert "agent-1" in graph.nodes
        assert graph.nodes["agent-1"]["name"] == "TestAgent"

    def test_graph_add_edge(self):
        """Should be able to add edges via add_edge method."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.add_edge("agent-1", "agent-2", "interaction", weight=1.0)
        assert len(graph.edges) == 1
        assert graph.edges[0]["from"] == "agent-1"
        assert graph.edges[0]["to"] == "agent-2"

    def test_graph_to_dict(self):
        """Graph should be serializable to dict."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph

        graph = KnowledgeGraph()
        graph.nodes["a1"] = {"id": "a1", "name": "Agent1", "type": "agent"}
        graph.edges.append({"source": "a1", "target": "a2", "type": "task"})

        d = graph.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert len(d["nodes"]) == 1
        assert len(d["edges"]) == 1


class TestProfileGenerator:
    """Test profile generation logic."""

    def test_generate_persona_from_agent(self):
        """Should generate a valid persona from agent data."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph
        from services.simulation.app.services.profile_generator import (
            generate_persona_from_agent,
        )

        agent_data = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": "TestAgent",
            "capabilities": [{"name": "compute"}],
            "reputation_tier": "gold",
            "success_rate": 0.95,
            "total_tasks_completed": 30,
        }
        graph = KnowledgeGraph()

        persona = generate_persona_from_agent(agent_data, graph, agent_index=0)
        assert "user_id" in persona
        assert persona["user_id"] == 0
        assert "name" in persona
        assert "personality" in persona

    def test_generate_persona_from_injection(self):
        """Should generate persona for injected (synthetic) agents."""
        from services.simulation.app.schemas import InjectedAgent
        from services.simulation.app.services.profile_generator import (
            generate_persona_from_injection,
        )

        ia = InjectedAgent(
            name="Malicious Bot",
            description="An attacker agent",
            personality_traits={"aggression": 0.9, "deception": 0.8},
        )
        persona = generate_persona_from_injection(ia, agent_index=10)

        assert persona["user_id"] == 10
        assert persona["is_injected"] is True
        assert "name" in persona

    def test_generate_all_profiles(self):
        """Should combine seed agents and injected agents."""
        from services.simulation.app.services.graph_builder import KnowledgeGraph
        from services.simulation.app.services.profile_generator import (
            generate_all_profiles,
        )

        seed_data = {
            "agents": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "name": "Agent1",
                    "capabilities": [],
                    "reputation_tier": "silver",
                    "success_rate": 0.8,
                    "total_tasks_completed": 15,
                },
                {
                    "id": "550e8400-e29b-41d4-a716-446655440001",
                    "name": "Agent2",
                    "capabilities": [],
                    "reputation_tier": "bronze",
                    "success_rate": 0.6,
                    "total_tasks_completed": 5,
                },
            ]
        }
        graph = KnowledgeGraph()

        profiles = generate_all_profiles(seed_data, graph, injected_agents=None)
        assert len(profiles) == 2
        assert profiles[0]["user_id"] == 0
        assert profiles[1]["user_id"] == 1

    def test_profiles_with_injected_agents(self):
        """Injected agents should be appended after seed agents."""
        from services.simulation.app.schemas import InjectedAgent
        from services.simulation.app.services.graph_builder import KnowledgeGraph
        from services.simulation.app.services.profile_generator import (
            generate_all_profiles,
        )

        seed_data = {
            "agents": [
                {
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "name": "Agent1",
                    "capabilities": [],
                    "reputation_tier": "unranked",
                    "success_rate": 0.5,
                    "total_tasks_completed": 2,
                },
            ]
        }
        graph = KnowledgeGraph()
        injected = [InjectedAgent(name="TestBot", description="observer agent")]

        profiles = generate_all_profiles(seed_data, graph, injected_agents=injected)
        assert len(profiles) == 2
        assert profiles[1]["is_injected"] is True


class TestCostCalculatorEdgeCases:
    """Additional cost calculator edge cases for seed scenarios."""

    def test_large_agent_count(self):
        """Cost should handle large agent counts without overflow."""
        from services.simulation.app.schemas import SeedConfig, SimulationParams
        from services.simulation.app.services.cost_calculator import estimate_cost

        cost = estimate_cost(SeedConfig(), SimulationParams(num_steps=100), num_seed_agents=1000)
        assert cost > 0
        assert isinstance(cost, (int, float))

    def test_minimal_simulation(self):
        """Minimal simulation should still produce a positive cost."""
        from services.simulation.app.schemas import SeedConfig, SimulationParams
        from services.simulation.app.services.cost_calculator import estimate_cost

        cost = estimate_cost(SeedConfig(), SimulationParams(num_steps=10), num_seed_agents=1)
        assert cost > 0
