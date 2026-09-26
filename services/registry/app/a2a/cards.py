"""A2A 1.0 Agent Cards built from the official SDK types (ADR-0009 D4).

* The canonical card (``/.well-known/agent-card.json``) describes the
  AgentNet network: two real interfaces with no tenant, free network skills.
* A per-agent card describes one marketplace agent through the SAME two
  interfaces with ``tenant = <agent id>``. It is built from public fields
  only: never the owner, the registered endpoint, the public key, a wallet,
  prompts, Society context or traces.

Every claim is true of the running gateway: streaming is real SSE, push
notifications and the extended card are not offered.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct

from . import config

AGENT_CARD_STATUSES = ("active", "unverified")
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_SKILLS = 64
IO_MODES = ["application/json", "text/plain"]
CARD_VERSION = "1.0.0"


def _status(agent: Any) -> str:
    value = agent.status
    return value.value if hasattr(value, "value") else str(value)


def card_available(agent: Any) -> bool:
    return agent is not None and _status(agent) in AGENT_CARD_STATUSES


def _interfaces(base_url: str, tenant: str = "") -> List[pb.AgentInterface]:
    return [
        pb.AgentInterface(
            url=f"{base_url}{config.JSONRPC_PATH}",
            protocol_binding=config.BINDING_JSONRPC,
            protocol_version=config.PROTOCOL_VERSION,
            tenant=tenant,
        ),
        pb.AgentInterface(
            url=f"{base_url}{config.HTTP_JSON_PATH}",
            protocol_binding=config.BINDING_HTTP_JSON,
            protocol_version=config.PROTOCOL_VERSION,
            tenant=tenant,
        ),
    ]


def _security() -> Dict[str, Any]:
    return {
        "security_schemes": {
            "bearer": pb.SecurityScheme(
                http_auth_security_scheme=pb.HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    bearer_format="JWT",
                    description=(
                        "An AgentNet credential: an agent JWT or an agent-scoped spt_ token "
                        "(with the execute action to create tasks), or a user JWT to read the "
                        "tasks of agents you own."
                    ),
                )
            )
        },
        "security_requirements": [pb.SecurityRequirement(schemes={"bearer": pb.StringList()})],
    }


def _struct(values: Dict[str, Any]) -> Struct:
    s = Struct()
    s.update(values)
    return s


def _economics_extension(skill_prices: Optional[Dict[str, int]] = None) -> pb.AgentExtension:
    params: Dict[str, Any] = {
        "currencies": ["credits", "usdc"],
        "metadataKey": config.ECONOMICS_EXTENSION_URI,
        "fields": ["maxBudget", "currency", "quotedPrice", "timeoutSeconds", "skillId"],
    }
    if skill_prices is not None:
        params["skillPrices"] = skill_prices
    return pb.AgentExtension(
        uri=config.ECONOMICS_EXTENSION_URI,
        description=(
            "AgentNet escrow economics. Paid skills require this extension: send the A2A-Extensions "
            "header and message.metadata[<uri>] = {maxBudget, currency}. The price is reserved in "
            "escrow and settled only when the agent completes the task; failure, timeout or a "
            "cancellation before the agent starts releases it. Free skills need no extension."
        ),
        required=False,
        params=_struct(params),
    )


def _federation_extension() -> pb.AgentExtension:
    return pb.AgentExtension(
        uri=config.FEDERATION_EXTENSION_URI,
        description=(
            "Delegation depth. Agents that forward work set message.metadata[<uri>] = {depth}; "
            "AgentNet refuses requests at or beyond its maximum depth."
        ),
        required=False,
        params=_struct({"maxDepth": config.max_federation_depth()}),
    )


def _capabilities(skill_prices: Optional[Dict[str, int]] = None) -> pb.AgentCapabilities:
    return pb.AgentCapabilities(
        streaming=True,
        push_notifications=False,
        extended_agent_card=False,
        extensions=[_economics_extension(skill_prices), _federation_extension()],
    )


def _provider() -> pb.AgentProvider:
    return pb.AgentProvider(organization="AgentNet", url=os.getenv("A2A_PROVIDER_URL", "https://agentnet.io.vn"))


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "")
    return text[:limit]


def network_card(base_url: str) -> pb.AgentCard:
    card = pb.AgentCard(
        name="AgentNet",
        description=(
            "AgentNet is a marketplace of AI agents with escrow-backed payments. This card is the "
            "network itself: search the marketplace or fetch an agent's card here, then talk to any "
            "marketplace agent through the same endpoints with tenant = its agent id."
        ),
        supported_interfaces=_interfaces(base_url),
        provider=_provider(),
        version=CARD_VERSION,
        capabilities=_capabilities(),
        default_input_modes=IO_MODES,
        default_output_modes=IO_MODES,
        skills=[
            pb.AgentSkill(
                id=config.SKILL_MARKETPLACE_SEARCH,
                name="Marketplace search",
                description=(
                    "Find marketplace agents. Send text (the query) or a JSON object "
                    "{query?, capability?, maxPrice?, limit?}. Free; answered immediately with a message."
                ),
                tags=["discovery", "marketplace", "free"],
                examples=["translation agents under 20 credits"],
                input_modes=IO_MODES,
                output_modes=IO_MODES,
            ),
            pb.AgentSkill(
                id=config.SKILL_AGENT_CARD,
                name="Agent card lookup",
                description="Return one marketplace agent's A2A card. Send {\"agentId\": \"<uuid>\"}. Free.",
                tags=["discovery", "free"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
        ],
        **_security(),
    )
    doc = os.getenv("A2A_DOCUMENTATION_URL", "").strip()
    if doc.startswith("https://"):
        card.documentation_url = doc
    return card


def _skill_input_modes(capability: Dict[str, Any]) -> List[str]:
    """``text/plain`` is advertised only when a text message can satisfy the
    capability's input schema (text arrives as ``{"text": ...}``)."""
    schema = capability.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
    if not schema or not props or ("text" in props and required <= {"text"}):
        return IO_MODES
    return ["application/json"]


def skill_price(capability: Dict[str, Any]) -> int:
    try:
        return max(0, int(capability.get("price", 0) or 0))
    except (TypeError, ValueError):
        return 0


def agent_card(agent: Any, base_url: str) -> pb.AgentCard:
    skills: List[pb.AgentSkill] = []
    prices: Dict[str, int] = {}
    for cap in list(agent.capabilities or [])[:MAX_SKILLS]:
        name = _clean_text(cap.get("name"), 128)
        if not name:
            continue
        price = skill_price(cap)
        prices[name] = price
        version = _clean_text(cap.get("version"), 32)
        skills.append(
            pb.AgentSkill(
                id=name,
                name=name,
                description=_clean_text(
                    cap.get("description") or f"{name}{' v' + version if version else ''} — "
                    f"{'free' if price == 0 else f'{price} per task (economics extension required)'}",
                    MAX_DESCRIPTION,
                ),
                tags=["agentnet", "free" if price == 0 else "paid"],
                input_modes=_skill_input_modes(cap),
                output_modes=IO_MODES,
            )
        )
    return pb.AgentCard(
        name=_clean_text(agent.name, MAX_NAME) or "AgentNet agent",
        description=_clean_text(agent.description, MAX_DESCRIPTION) or "An AgentNet marketplace agent.",
        supported_interfaces=_interfaces(base_url, tenant=str(agent.id)),
        provider=_provider(),
        version=CARD_VERSION,
        capabilities=_capabilities(prices),
        default_input_modes=IO_MODES,
        default_output_modes=IO_MODES,
        skills=skills,
        **_security(),
    )


def card_json(card: pb.AgentCard) -> Dict[str, Any]:
    return MessageToDict(card, preserving_proto_field_name=False)


def card_etag(payload: Dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f'"{digest[:32]}"'
