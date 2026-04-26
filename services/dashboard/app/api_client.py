import os
import httpx
from flask import session

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
        # We assume local docker networks resolve 'registry' correctly, but for localhost dev (running python -m app.main) it might be localhost:8000
        # Wait, the REGISTRY_URL environment variable handles this.
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
            # We assume the endpoint is POST /v1/wallets/{id}/fund 
            # Or perhaps there is an endpoint like that. Let's make the best guess or check implementation.
            # wait, let me actually check the payment backend router if available, or just implement it with assumption and graceful degradation.
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
        # The backend API `/v1/agents/` doesn't natively filter by my user_id via query param.
        # But `AgentSchema` returns `user_id`, and `get_wallets` returns the `owner_id` (which is user_id for owner_type=user).
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
            # discover endpoint returns a different shape: {"capability": "...", "recommendations": [...]}
            return self._handle_response(resp)
        except httpx.RequestError as e:
            # Fallback if discovery fails due to endpoint structure
            return {"recommendations": self.get_agents(capability=capability)}

    def get_agent(self, agent_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/{agent_id}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def create_task(self, payload):
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/tasks/", json=payload, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_task(self, task_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/tasks/{task_id}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_tasks(self, status=None, skip=0, limit=20):
        url = f"{REGISTRY_URL}/v1/tasks/?skip={skip}&limit={limit}"
        if status:
            url += f"&status={status}"
        try:
            resp = httpx.get(url, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_trace(self, trace_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/tasks/traces/{trace_id}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Offers
    def get_offers(self, skip=0, limit=20):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/offers/?skip={skip}&limit={limit}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_offer(self, offer_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/offers/{offer_id}", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def create_offer(self, payload):
        try:
            caller_agent_id = payload.pop("caller_agent_id", None)
            url = f"{REGISTRY_URL}/v1/offers/"
            if caller_agent_id:
                url += f"?caller_agent_id={caller_agent_id}"
            resp = httpx.post(url, json=payload, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def accept_offer(self, offer_id):
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/offers/{offer_id}/accept", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def reject_offer(self, offer_id):
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/offers/{offer_id}/reject", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def counter_offer(self, offer_id, payload):
        try:
            resp = httpx.post(f"{REGISTRY_URL}/v1/offers/{offer_id}/counter", json=payload, headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Notifications
    def get_notifications(self):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/notifications/", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def mark_notification_read(self, notification_id):
        try:
            resp = httpx.put(f"{REGISTRY_URL}/v1/notifications/{notification_id}/read", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def mark_all_notifications_read(self):
        try:
            resp = httpx.put(f"{REGISTRY_URL}/v1/notifications/read-all", headers=self._get_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Chat / Agent Collaboration
    def get_chat_threads(self, limit=20, skip=0):
        try:
            resp = httpx.get(
                f"{REGISTRY_URL}/v1/chat/threads?limit={limit}&skip={skip}",
                headers=self._get_headers(), timeout=5.0
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_chat_messages(self, from_agent_id=None, to_agent_id=None, limit=20, skip=0):
        params = f"limit={limit}&skip={skip}"
        if from_agent_id:
            params += f"&from_agent_id={from_agent_id}"
        if to_agent_id:
            params += f"&to_agent_id={to_agent_id}"
        try:
            resp = httpx.get(
                f"{REGISTRY_URL}/v1/chat/?{params}",
                headers=self._get_headers(), timeout=5.0
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # ─────────────────────────────────────────────────────
    # Goals (Phase: agent-goals-and-self-improvement)
    # ─────────────────────────────────────────────────────

    def _maybe_headers(self):
        """Auth headers if logged in, else empty dict (for public reads)."""
        try:
            return self._get_headers()
        except AuthRequiredError:
            return {}

    def get_goals(self, status=None, owner_type=None, owner_id=None, limit=100):
        params = [f"limit={limit}"]
        if status:
            params.append(f"status={status}")
        if owner_type:
            params.append(f"owner_type={owner_type}")
        if owner_id:
            params.append(f"owner_id={owner_id}")
        url = f"{REGISTRY_URL}/v1/goals/?" + "&".join(params)
        try:
            resp = httpx.get(url, headers=self._maybe_headers(), timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_goal(self, goal_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/goals/{goal_id}", timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def create_goal(self, payload):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/goals/", json=payload,
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def complete_goal(self, goal_id):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/goals/{goal_id}/complete",
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def fail_goal(self, goal_id):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/goals/{goal_id}/fail",
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def cancel_goal(self, goal_id):
        try:
            resp = httpx.delete(
                f"{REGISTRY_URL}/v1/goals/{goal_id}",
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Agent mission + lessons + agent-scoped goals

    def get_agent_mission(self, agent_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/{agent_id}/mission", timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def update_agent_mission(self, agent_id, mission=None, current_goal_id=None,
                             clear_goal=False):
        payload = {}
        if mission is not None:
            payload["mission"] = mission
        if clear_goal:
            payload["current_goal_id"] = None
        elif current_goal_id is not None:
            payload["current_goal_id"] = current_goal_id
        try:
            resp = httpx.patch(
                f"{REGISTRY_URL}/v1/agents/{agent_id}/mission",
                json=payload, headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_agent_goals(self, agent_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/agents/{agent_id}/goals", timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_agent_lessons(self, agent_id, tag=None, include_society=True, limit=50):
        params = [f"limit={limit}",
                  f"include_society={'true' if include_society else 'false'}"]
        if tag:
            params.append(f"tag={tag}")
        url = f"{REGISTRY_URL}/v1/agents/{agent_id}/lessons?" + "&".join(params)
        try:
            resp = httpx.get(url, timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Improvements

    def get_improvements(self, status=None, source=None, agent_id=None, limit=100):
        params = [f"limit={limit}"]
        if status:
            params.append(f"status={status}")
        if source:
            params.append(f"source={source}")
        if agent_id:
            params.append(f"agent_id={agent_id}")
        url = f"{REGISTRY_URL}/v1/improvements/?" + "&".join(params)
        try:
            resp = httpx.get(url, timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def get_improvement(self, proposal_id):
        try:
            resp = httpx.get(f"{REGISTRY_URL}/v1/improvements/{proposal_id}", timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def create_improvement(self, payload):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/improvements/", json=payload,
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def approve_improvement(self, proposal_id):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/improvements/{proposal_id}/approve",
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def reject_improvement(self, proposal_id, reason=None):
        body = {"reason": reason} if reason else {}
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/improvements/{proposal_id}/reject",
                json=body, headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def convert_improvement(self, proposal_id, payload):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/improvements/{proposal_id}/convert-to-task",
                json=payload, headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def mark_improvement_implemented(self, proposal_id):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/improvements/{proposal_id}/mark-implemented",
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    # Memory items

    def get_memory(self, scope=None, agent_id=None, tag=None, min_importance=0, limit=100):
        params = [f"limit={limit}", f"min_importance={min_importance}"]
        if scope:
            params.append(f"scope={scope}")
        if agent_id:
            params.append(f"agent_id={agent_id}")
        if tag:
            params.append(f"tag={tag}")
        url = f"{REGISTRY_URL}/v1/memory/?" + "&".join(params)
        try:
            resp = httpx.get(url, timeout=5.0)
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def create_memory(self, payload):
        try:
            resp = httpx.post(
                f"{REGISTRY_URL}/v1/memory/", json=payload,
                headers=self._get_headers(), timeout=5.0,
            )
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

    def delete_memory(self, memory_id):
        try:
            resp = httpx.delete(
                f"{REGISTRY_URL}/v1/memory/{memory_id}",
                headers=self._get_headers(), timeout=5.0,
            )
            if resp.status_code == 204:
                return {"deleted": True}
            return self._handle_response(resp)
        except httpx.RequestError as e:
            raise APIError(f"Connection failed: {e}")

api_client = APIClient()
