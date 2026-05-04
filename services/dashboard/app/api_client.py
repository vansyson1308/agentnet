import requests
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import os

logger = logging.getLogger(__name__)

class APIError(Exception):
    def __init__(self, message, status_code=500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class AuthRequiredError(APIError):
    def __init__(self, message="Authentication required"):
        super().__init__(message, status_code=401)

class ApiClient:
    """Client for the AgentNet backend API."""

    def __init__(self, base_url=None):
        self.base_url = base_url or os.getenv("API_BASE_URL", "http://localhost:8000")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", 10)
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {method} {path} - {e}")
            raise APIError(str(e), getattr(e.response, 'status_code', 500))

    def health_registry(self, timeout=2.0) -> bool:
        try:
            self._request("GET", "/health", timeout=timeout)
            return True
        except:
            return False

    def fetch_agents(self, search=None, category=None, sort=None, order=None, limit=100):
        """Fetch agents from the public endpoint (no authentication required)."""
        params = {"limit": limit}
        if search:
            params["search"] = search
        if category:
            params["category"] = category
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        data = self._request("GET", "/agents", params=params)
        return data.get("agents", data if isinstance(data, list) else [])

    def fetch_agent(self, agent_id: str) -> dict:
        data = self._request("GET", f"/agents/{agent_id}")
        return data.get("agent", data)

    # ... [other methods remain unchanged] ...

api_client = ApiClient()