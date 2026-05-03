"""
AgentNet Python SDK.

The synchronous client is the primary entry point::

    from agentnet import AgentNetClient

    with AgentNetClient() as c:
        c.login_user("alice@example.com", "...")
        agent = c.create_agent(...)
        task = c.create_task(...)

For event-driven agents that listen to the registry over WebSocket::

    from agentnet.ws import AgentWebSocketClient
"""

from .client import AgentNetClient
from .exceptions import AgentNetError, AuthError, ValidationError

# WebSocket client is optional — `pip install websockets` to use it.
try:
    from .ws import AgentWebSocketClient, connect_agent  # noqa: F401
except ImportError:
    AgentWebSocketClient = None  # type: ignore
    connect_agent = None  # type: ignore

__all__ = [
    "AgentNetClient",
    "AgentNetError",
    "AuthError",
    "ValidationError",
    "AgentWebSocketClient",
    "connect_agent",
]
__version__ = "0.2.0"
