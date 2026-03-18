"""
AgentNet Client - Main SDK class for interacting with AgentNet services.
"""

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


class AgentNetError(Exception):
    """Base exception for AgentNet errors."""

    pass


class AuthError(AgentNetError):
    """Authentication error."""

    pass


class ValidationError(AgentNetError):
    """Validation error."""

    pass


@dataclass
class User:
    """User data."""

    id: str
    email: str


@dataclass
class Agent:
    """Agent data."""

    id: str
    name: str
    description: str
    capabilities: List[Dict[str, Any]]
    endpoint: str
    status: str


@dataclass
class Wallet:
    """Wallet data."""

    id: str
    balance_credits: int
    balance_usdc: float
    reserved_credits: int
    reserved_usdc: float
    spending_cap: int
    daily_spent: int


@dataclass
class TaskSession:
    """Task session data."""

    id: str
    trace_id: str
    status: str
    capability: str
    escrow_amount: int
    currency: str


class AgentNetClient:
    """
    Main client for AgentNet services.

    Usage:
        client = AgentNetClient()
        client.register_user("user@example.com", "password123")
        client.login_user("user@example.com", "password123")
        agent = client.create_agent(...)
        wallet = client.get_wallet(...)
    """

    def __init__(self, registry_url: str = None, payment_url: str = None, timeout: float = 30.0):
        """
        Initialize the client.

        Args:
            registry_url: Registry service URL (default: http://localhost:8000)
            payment_url: Payment service URL (default: http://localhost:8001)
            timeout: Request timeout in seconds
        """
        self.registry_url = registry_url or os.getenv("REGISTRY_URL", "http://localhost:8000")
        self.payment_url = payment_url or os.getenv("PAYMENT_URL", "http://localhost:8001")
        self.timeout = timeout

        self._user_token: Optional[str] = None
        self._agent_token: Optional[str] = None
        self._user: Optional[User] = None
        self._agent: Optional[Agent] = None

        self._client = httpx.Client(timeout=timeout)

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ─────────────────────────────────────────────────────────
    # User Authentication
    # ─────────────────────────────────────────────────────────

    def register_user(self, email: str, password: str, phone: str = None) -> User:
        """
        Register a new user.

        Args:
            email: User email
            password: User password (min 8 chars, must have upper, lower, digit)
            phone: Optional phone number

        Returns:
            User object
        """
        response = self._client.post(
            f"{self.registry_url}/v1/auth/user/register",
            json={"email": email, "password": password, "phone": phone},
        )

        if response.status_code == 400:
            # User might already exist
            raise ValidationError(response.json().get("detail", "Registration failed"))

        if response.status_code != 201:
            raise AgentNetError(f"Registration failed: {response.status_code} {response.text}")

        data = response.json()
        return User(id=data["id"], email=data["email"])

    def login_user(self, email: str, password: str) -> User:
        """
        Login as user.

        Args:
            email: User email
            password: User password

        Returns:
            User object
        """
        response = self._client.post(
            f"{self.registry_url}/v1/auth/user/login",
            data={"username": email, "password": password},
        )

        if response.status_code != 200:
            raise AuthError(f"Login failed: {response.status_code} {response.text}")

        data = response.json()
        self._user_token = data["access_token"]
        self._user = User(id=data.get("user_id", ""), email=email)

        return self._user

    def get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers."""
        if self._agent_token:
            return {"Authorization": f"Bearer {self._agent_token}"}
        if self._user_token:
            return {"Authorization": f"Bearer {self._user_token}"}
        raise AuthError("Not authenticated")

    # ─────────────────────────────────────────────────────────
    # Agent Management
    # ─────────────────────────────────────────────────────────

    def create_agent(
        self,
        name: str,
        description: str,
        capabilities: List[Dict[str, Any]],
        endpoint: str,
        public_key: str,
    ) -> Agent:
        """
        Create a new agent.

        Args:
            name: Agent name
            description: Agent description
            capabilities: List of capabilities
            endpoint: Agent endpoint URL
            public_key: Agent's public key

        Returns:
            Agent object
        """
        response = self._client.post(
            f"{self.registry_url}/v1/agents/",
            json={
                "name": name,
                "description": description,
                "capabilities": capabilities,
                "endpoint": endpoint,
                "public_key": public_key,
            },
            headers=self.get_auth_headers(),
        )

        if response.status_code == 400:
            # Might already exist - try to get it
            return self.get_agent_by_name(name)

        if response.status_code != 201:
            raise AgentNetError(f"Agent creation failed: {response.status_code} {response.text}")

        data = response.json()
        return self._parse_agent(data)

    def get_agent_by_name(self, name: str) -> Agent:
        """Get agent by name."""
        response = self._client.get(f"{self.registry_url}/v1/agents/", headers=self.get_auth_headers())

        if response.status_code != 200:
            raise AgentNetError(f"Failed to list agents: {response.status_code}")

        agents = response.json()
        for agent in agents:
            if agent.get("name") == name:
                return self._parse_agent(agent)

        raise AgentNetError(f"Agent not found: {name}")

    def get_agent(self, agent_id: str) -> Agent:
        """Get agent by ID."""
        response = self._client.get(f"{self.registry_url}/v1/agents/{agent_id}", headers=self.get_auth_headers())

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get agent: {response.status_code}")

        return self._parse_agent(response.json())

    def search_agents(self, capability: str = None, min_rating: int = None, max_price: float = None) -> List[Agent]:
        """Search for agents."""
        params = {}
        if capability:
            params["capability"] = capability
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_price is not None:
            params["max_price"] = max_price

        response = self._client.get(
            f"{self.registry_url}/v1/agents/",
            params=params,
            headers=self.get_auth_headers(),
        )

        if response.status_code != 200:
            raise AgentNetError(f"Failed to search agents: {response.status_code}")

        return [self._parse_agent(a) for a in response.json()]

    def _parse_agent(self, data: Dict) -> Agent:
        """Parse agent data."""
        return Agent(
            id=str(data["id"]),
            name=data["name"],
            description=data.get("description", ""),
            capabilities=data.get("capabilities", []),
            endpoint=data.get("endpoint", ""),
            status=data.get("status", "unknown"),
        )

    # ─────────────────────────────────────────────────────────
    # A2A Agent Card (Discovery)
    # ─────────────────────────────────────────────────────────

    def get_agent_card(self, agent_id: str) -> Dict[str, Any]:
        """
        Get the A2A Agent Card for a registered agent.

        Returns the standard A2A-compatible JSON card describing
        the agent's capabilities, endpoint, and auth requirements.
        """
        response = self._client.get(f"{self.registry_url}/v1/agents/{agent_id}/a2a-card")

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get agent card: {response.status_code}")

        return response.json()

    def get_registry_card(self) -> Dict[str, Any]:
        """
        Get the A2A Agent Card for the AgentNet Registry itself.

        Describes what the registry offers and how to interact with it.
        """
        response = self._client.get(f"{self.registry_url}/.well-known/agent-card.json")

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get registry card: {response.status_code}")

        return response.json()

    def fetch_remote_agent_card(self, base_url: str) -> Dict[str, Any]:
        """
        Fetch an A2A Agent Card from any remote URL.

        Tries /.well-known/agent-card.json per the A2A spec.

        Args:
            base_url: Base URL of the remote agent (e.g., "https://agent.example.com")

        Returns:
            A2A Agent Card as dict
        """
        url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        response = self._client.get(url, timeout=10.0)

        if response.status_code != 200:
            raise AgentNetError(f"No A2A card at {url}: {response.status_code}")

        return response.json()

    # ─────────────────────────────────────────────────────────
    # Wallet Management
    # ─────────────────────────────────────────────────────────

    def get_wallet(self, wallet_id: str) -> Wallet:
        """Get wallet by ID."""
        response = self._client.get(
            f"{self.payment_url}/v1/wallets/{wallet_id}",
            headers=self.get_auth_headers(),
        )

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get wallet: {response.status_code}")

        return self._parse_wallet(response.json())

    def get_agent_wallet(self, agent_id: str = None) -> Wallet:
        """Get wallet for an agent (defaults to current agent)."""
        if agent_id:
            agent = self.get_agent(agent_id)
        elif self._agent:
            agent = self._agent
        else:
            raise AgentNetError("No agent specified and no agent logged in")

        response = self._client.get(f"{self.payment_url}/v1/wallets/", headers=self.get_auth_headers())

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get wallets: {response.status_code}")

        wallets = response.json()
        for wallet in wallets:
            if wallet.get("owner_type") == "agent" and wallet.get("owner_id") == agent.id:
                return self._parse_wallet(wallet)

        raise AgentNetError("No wallet found for agent")

    def _parse_wallet(self, data: Dict) -> Wallet:
        """Parse wallet data."""
        return Wallet(
            id=str(data["id"]),
            balance_credits=data.get("balance_credits", 0),
            balance_usdc=float(data.get("balance_usdc", 0)),
            reserved_credits=data.get("reserved_credits", 0),
            reserved_usdc=float(data.get("reserved_usdc", 0)),
            spending_cap=data.get("spending_cap", 0),
            daily_spent=data.get("daily_spent", 0),
        )

    # ─────────────────────────────────────────────────────────
    # Task Sessions
    # ─────────────────────────────────────────────────────────

    def create_task(
        self,
        caller_agent_id: str,
        callee_agent_id: str,
        capability: str,
        input_data: Dict[str, Any],
        max_budget: int,
        currency: str = "credits",
        timeout_seconds: int = 300,
    ) -> TaskSession:
        """
        Create a task session.

        Args:
            caller_agent_id: ID of the caller agent
            callee_agent_id: ID of the callee agent
            capability: Capability name to invoke
            input_data: Input data for the task
            max_budget: Maximum budget
            currency: Currency (credits or usdc)
            timeout_seconds: Task timeout

        Returns:
            TaskSession object
        """
        response = self._client.post(
            f"{self.registry_url}/v1/tasks/",
            json={
                "caller_agent_id": caller_agent_id,
                "callee_agent_id": callee_agent_id,
                "capability": capability,
                "input": input_data,
                "max_budget": max_budget,
                "currency": currency,
                "timeout_seconds": timeout_seconds,
            },
            headers=self.get_auth_headers(),
        )

        if response.status_code != 201:
            raise AgentNetError(f"Task creation failed: {response.status_code} {response.text}")

        data = response.json()
        return TaskSession(
            id=data["task_session_id"],
            trace_id=data["trace_id"],
            status="initiated",
            capability=capability,
            escrow_amount=max_budget,
            currency=currency,
        )

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """Get task details."""
        response = self._client.get(f"{self.registry_url}/v1/tasks/{task_id}", headers=self.get_auth_headers())

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get task: {response.status_code}")

        return response.json()

    def confirm_task(self, task_id: str, output: Dict[str, Any]) -> Dict[str, Any]:
        """Confirm task completion."""
        response = self._client.put(
            f"{self.registry_url}/v1/tasks/{task_id}/confirm",
            json=output,
            headers=self.get_auth_headers(),
        )

        if response.status_code != 200:
            raise AgentNetError(f"Task confirmation failed: {response.status_code} {response.text}")

        return response.json()

    def fail_task(self, task_id: str, error_message: str) -> Dict[str, Any]:
        """Mark task as failed."""
        response = self._client.put(
            f"{self.registry_url}/v1/tasks/{task_id}/fail",
            params={"error_message": error_message},
            headers=self.get_auth_headers(),
        )

        if response.status_code != 200:
            raise AgentNetError(f"Task failure failed: {response.status_code} {response.text}")

        return response.json()

    # ─────────────────────────────────────────────────────────
    # Tracing
    # ─────────────────────────────────────────────────────────

    def get_trace(self, trace_id: str) -> Dict[str, Any]:
        """Get trace (spans) for a task."""
        response = self._client.get(
            f"{self.registry_url}/v1/tasks/traces/{trace_id}",
            headers=self.get_auth_headers(),
        )

        if response.status_code != 200:
            raise AgentNetError(f"Failed to get trace: {response.status_code}")

        return response.json()

    # ─────────────────────────────────────────────────────────
    # Dev-Only: Fund Wallet (only in development mode)
    # ─────────────────────────────────────────────────────────

    def dev_fund_wallet(self, wallet_id: str, amount: int, currency: str = "credits") -> Dict[str, Any]:
        """
        Add funds to a wallet (DEV ONLY - requires ENV=development).

        This is a convenience method for development/testing.
        """
        response = self._client.post(
            f"{self.payment_url}/v1/wallets/{wallet_id}/fund",
            json={"amount": amount, "currency": currency},
            headers=self.get_auth_headers(),
        )

        if response.status_code == 404:
            # Endpoint might not exist - try direct DB approach
            raise AgentNetError("Funding endpoint not available. Use direct DB or ensure services are running.")

        if response.status_code == 403:
            raise AgentNetError("Funding not allowed in production mode")

        if response.status_code != 200:
            raise AgentNetError(f"Funding failed: {response.status_code} {response.text}")

        return response.json()
