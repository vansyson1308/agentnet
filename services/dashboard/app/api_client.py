import os
import httpx
from flask import session
from datetime import datetime, timedelta, timezone

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8001")

class APIError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code

class AuthRequiredError(Exception):
    pass

class APIClient:
    def health_registry(self, timeout: float = 2.0) -> bool:
        """Used by dashboard /readyz to confirm the upstream registry
        is reachable. Doesn't raise — caller treats False as "not ready"."""
        try:
            r = httpx.get(f"{REGISTRY_URL}/healthz", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _get_headers(self):
        token = session.get("access_token")
        if not token:
            raise AuthRequiredError()
        return {
            "Authorization": f"Bearer {token}"
        }

    def _handle_response(self, resp):
        if resp.status_code in (401, 403):
            raise AuthRequiredError()
        if resp.status_code >= 400:
            error_msg = "API Error"
            try:
                data = resp.json()
                if "detail" in data:
                    if isinstance(data["detail"], list):
                        error_msg = str(data["detail"])
                    else:
                        error_msg = data["detail"]
            except:
                error_msg = resp.text
            raise APIError(error_msg, resp.status_code)
        return resp.json()

    def login(self, username, password):
        data = {"username": username, "password": password}
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/auth/user/login", data=data, timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def register(self, email, password):
        data = {"email": email, "password": password}
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/auth/user/register", json=data, timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_wallets(self):
        try:
            resp = httpx.get(f"{PAYMENT_URL}/v1/wallets/", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_transactions(self):
        try:
            resp = httpx.get(f"{PAYMENT_URL}/v1/transactions/", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def fund_wallet(self, wallet_id, amount):
        data = {"amount": amount, "currency": "credits"}
        try:
            resp = httpx.post(f"{PAYMENT_URL}/v1/wallets/{wallet_id}/fund", json=data, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_agents(self, capability=None, limit=1000):
        url = f"{REGISTRY_URL}/v1/agents/?limit={limit}"
        if capability:
            url += f"&capability={capability}"
        try:
            resp = httpx.get(url, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_my_agents(self):
        wallets = self.get_wallets()
        user_id = next((w.get("owner_id") for w in wallets if w.get("owner_type") == "user"), None)
        all_agents = self.get_agents()
        if user_id:
            return [a for a in all_agents if a.get("user_id") == user_id]
        return []

    def create_agent(self, data):
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/agents/", json=data, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def discover_agents(self, capability):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/discover/{capability}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            return {"recommendations": self.get_agents(capability=capability)}

    def get_agent(self, agent_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/{agent_id}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_tasks(self):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/tasks/", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def fetch_agents(self, search=None, category=None, sort=None, order=None):
        """Fetch agents from the public endpoint (no authentication required)."""
        params = {}
        if search:
            params["search"] = search
        if category:
            params["category"] = category
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        try:
            # Public endpoint – no auth headers
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/", params=params, timeout=5.0)
            if resp.status_code >= 400:
                error_msg = "API Error"
                try:
                    data = resp.json()
                    if "detail" in data:
                        if isinstance(data["detail"], list):
                            error_msg = str(data["detail"])
                        else:
                            error_msg = data["detail"]
                except:
                    error_msg = resp.text
                raise APIError(error_msg, resp.status_code)
            return resp.json()
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

api_client = APIClient()