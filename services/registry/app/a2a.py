"""
A2A (Agent-to-Agent) Protocol support for AgentNet.

Implements Google's A2A Agent Card spec for agent discovery and interoperability.
Agent Cards are served at /.well-known/agent-card.json per RFC 8615.

See: https://a2a-protocol.org/latest/specification/
"""

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --- A2A Agent Card Pydantic Models ---


class A2AProvider(BaseModel):
    """Organization/individual that provides the agent."""

    name: str
    email: Optional[str] = None
    website: Optional[str] = None


class A2ACapabilities(BaseModel):
    """Supported protocol features."""

    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class A2ASkill(BaseModel):
    """A single skill/capability the agent can perform."""

    id: str
    name: str
    description: str
    tags: List[str] = []
    examples: List[str] = []
    inputModes: List[str] = Field(default_factory=lambda: ["application/json"])
    outputModes: List[str] = Field(default_factory=lambda: ["application/json"])


class A2ASecurityScheme(BaseModel):
    """Authentication scheme definition."""

    type: str  # "apiKey", "oauth2", "bearer", "none"
    name: Optional[str] = None
    in_: Optional[str] = Field(None, alias="in")  # "header", "query"
    description: Optional[str] = None
    tokenUrl: Optional[str] = None
    scopes: Optional[List[str]] = None

    model_config = {"populate_by_name": True}


class A2ASupportedInterface(BaseModel):
    """Protocol endpoint for communicating with this agent."""

    protocol: str = "JSONRPC"
    url: str


class A2AAgentCard(BaseModel):
    """
    A2A Agent Card — the public identity document for an AI agent.

    Served at /.well-known/agent-card.json to enable discovery
    by any A2A-compatible system.
    """

    name: str
    description: str
    version: str = "1.0.0"
    documentationUrl: Optional[str] = None
    iconUrl: Optional[str] = None
    provider: Optional[A2AProvider] = None
    capabilities: A2ACapabilities = Field(default_factory=A2ACapabilities)
    defaultInputModes: List[str] = Field(default_factory=lambda: ["application/json"])
    defaultOutputModes: List[str] = Field(default_factory=lambda: ["application/json"])
    supportedInterfaces: List[A2ASupportedInterface] = []
    skills: List[A2ASkill] = []
    securitySchemes: Dict[str, A2ASecurityScheme] = {}


# --- Conversion: AgentNet Agent → A2A Agent Card ---


def agent_to_a2a_card(
    agent_db,
    base_url: Optional[str] = None,
) -> A2AAgentCard:
    """
    Convert an AgentNet Agent (SQLAlchemy model) to an A2A Agent Card.

    Maps:
      - Agent.capabilities → A2A skills
      - Agent.endpoint → A2A supportedInterfaces
      - Agent.public_key → A2A securitySchemes (bearer)
    """
    if base_url is None:
        base_url = os.getenv("REGISTRY_PUBLIC_URL", "http://localhost:8000")

    # Convert AgentNet capabilities to A2A skills
    skills = []
    for cap in agent_db.capabilities or []:
        skill = A2ASkill(
            id=cap.get("name", "unknown"),
            name=cap.get("name", "unknown"),
            description=f"Capability: {cap.get('name', 'unknown')} v{cap.get('version', '1.0')}",
            tags=[cap.get("name", "")],
            examples=[],
            inputModes=["application/json"],
            outputModes=["application/json"],
        )
        skills.append(skill)

    # Build supported interfaces
    interfaces = []
    if agent_db.endpoint:
        interfaces.append(
            A2ASupportedInterface(
                protocol="JSONRPC",
                url=agent_db.endpoint,
            )
        )

    # Build security schemes
    security_schemes: Dict[str, A2ASecurityScheme] = {}
    if agent_db.public_key:
        security_schemes["bearer"] = A2ASecurityScheme(
            type="bearer",
            description="Agent public key verification",
        )
    else:
        security_schemes["none"] = A2ASecurityScheme(
            type="none",
            description="No authentication required",
        )

    return A2AAgentCard(
        name=agent_db.name,
        description=agent_db.description or f"AgentNet agent: {agent_db.name}",
        version="1.0.0",
        provider=A2AProvider(
            name="AgentNet",
            website=base_url,
        ),
        capabilities=A2ACapabilities(
            streaming=True,  # AgentNet supports WebSocket streaming
            pushNotifications=True,  # Redis pub/sub notifications
            stateTransitionHistory=True,  # Task state tracking
        ),
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
        supportedInterfaces=interfaces,
        skills=skills,
        securitySchemes=security_schemes,
    )


def build_registry_card(base_url: Optional[str] = None) -> A2AAgentCard:
    """
    Build an A2A Agent Card for the AgentNet Registry itself.

    This is served at /.well-known/agent-card.json and describes
    the registry as an A2A-compatible service.
    """
    if base_url is None:
        base_url = os.getenv("REGISTRY_PUBLIC_URL", "http://localhost:8000")

    return A2AAgentCard(
        name="AgentNet Registry",
        description=(
            "AgentNet Protocol v2.0 — AI Agent Marketplace with escrow-based payments, "
            "task execution, and real-time WebSocket communication."
        ),
        version="2.0.0",
        documentationUrl=f"{base_url}/docs",
        provider=A2AProvider(
            name="AgentNet",
            website=base_url,
        ),
        capabilities=A2ACapabilities(
            streaming=True,
            pushNotifications=True,
            stateTransitionHistory=True,
        ),
        defaultInputModes=["application/json"],
        defaultOutputModes=["application/json"],
        supportedInterfaces=[
            A2ASupportedInterface(protocol="JSONRPC", url=f"{base_url}/api/v1/ws"),
            A2ASupportedInterface(protocol="REST", url=f"{base_url}/api/v1"),
        ],
        skills=[
            A2ASkill(
                id="agent_discovery",
                name="Agent Discovery",
                description="Search and discover AI agents by capability, rating, and price",
                tags=["discovery", "search", "marketplace"],
                examples=["Find agents that can translate text", "Search for image processing agents"],
            ),
            A2ASkill(
                id="task_execution",
                name="Task Execution",
                description="Execute tasks with escrow-based payment and real-time updates",
                tags=["execution", "escrow", "payment"],
                examples=["Execute a translation task", "Run data processing with escrow"],
            ),
            A2ASkill(
                id="agent_registration",
                name="Agent Registration",
                description="Register new AI agents with capabilities and endpoint",
                tags=["registration", "onboarding"],
                examples=["Register a new translation agent"],
            ),
        ],
        securitySchemes={
            "bearer": A2ASecurityScheme(
                type="bearer",
                description="JWT Bearer token (obtain via /api/v1/auth/login)",
            ),
        },
    )
