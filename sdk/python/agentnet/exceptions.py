"""
AgentNet exceptions.
"""


class AgentNetError(Exception):
    """Base exception for AgentNet errors."""
    pass


class AuthError(AgentNetError):
    """Authentication error."""
    pass


class ValidationError(AgentNetError):
    """Validation error."""
    pass
