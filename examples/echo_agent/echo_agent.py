"""
Echo Agent - Sample AgentNet agent that echoes input.

This agent demonstrates:
- Agent registration with capability
- Polling for task sessions
- Returning deterministic response

Usage:
    python echo_agent.py --user-email user@example.com --password pass123 --name echo_agent
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

# Add SDK to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sdk" / "python"))

from agentnet import AgentNetClient


class EchoAgent:
    """
    Simple echo agent that responds with the input data.
    """

    CAPABILITY = {
        "name": "echo",
        "description": "Echoes back the input data",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"}
            },
            "required": ["message"]
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "echoed": {"type": "string"},
                "original": {"type": "object"}
            }
        },
        "price": 1  # 1 credit per call
    }

    def __init__(self, client: AgentNetClient, name: str, endpoint: str):
        self.client = client
        self.name = name
        self.endpoint = endpoint
        self.agent = None
        self.wallet = None
        self.polling_interval = 2  # seconds

    def register(self) -> None:
        """Register the agent."""
        print(f"Registering agent: {self.name}")

        try:
            self.agent = self.client.create_agent(
                name=self.name,
                description="Echo agent - returns input as output",
                capabilities=[self.CAPABILITY],
                endpoint=self.endpoint,
                public_key=str(uuid.uuid4())  # Placeholder
            )
            print(f"Agent registered: {self.agent.id}")
        except Exception as e:
            print(f"Agent might already exist: {e}")
            self.agent = self.client.get_agent_by_name(self.name)
            print(f"Using existing agent: {self.agent.id}")

        # Get wallet
        self.wallet = self.client.get_agent_wallet(self.agent.id)
        print(f"Wallet: {self.wallet.id}")
        print(f"Balance: {self.wallet.balance_credits} credits")

    def run(self) -> None:
        """Main loop - poll for tasks."""
        print(f"\nAgent {self.name} listening for tasks...")
        print(f"Endpoint: {self.endpoint}")
        print(f"Capability: {self.CAPABILITY['name']} (price: {self.CAPABILITY['price']} credits)")

        while True:
            try:
                # Search for tasks where this agent is the callee
                # Note: In a real implementation, you'd use WebSocket or push notification
                # For simplicity, we'll just log that we're ready
                pass

                # In a full implementation, you would:
                # 1. Connect to WebSocket for real-time notifications
                # 2. Or poll a /tasks/pending endpoint

                print(f"[{time.strftime('%H:%M:%S')}] Waiting for tasks...")
                time.sleep(self.polling_interval)

            except KeyboardInterrupt:
                print("\nShutting down...")
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(self.polling_interval)

    def process_task(self, task_data: dict) -> dict:
        """Process a task and return the result."""
        input_data = task_data.get("input", {})
        message = input_data.get("message", "")

        return {
            "echoed": message,
            "original": input_data
        }


def main():
    parser = argparse.ArgumentParser(description="Echo Agent for AgentNet")
    parser.add_argument("--user-email", required=True, help="User email for authentication")
    parser.add_argument("--password", required=True, help="User password")
    parser.add_argument("--name", default="echo_agent", help="Agent name")
    parser.add_argument("--endpoint", default="http://localhost:9000", help="Agent endpoint")
    parser.add_argument("--registry-url", default="http://localhost:8000", help="Registry service URL")
    parser.add_argument("--payment-url", default="http://localhost:8001", help="Payment service URL")
    parser.add_argument("--poll-interval", type=int, default=2, help="Polling interval in seconds")

    args = parser.parse_args()

    # Initialize client
    client = AgentNetClient(
        registry_url=args.registry_url,
        payment_url=args.payment_url
    )

    try:
        # Login
        print("Logging in...")
        client.login_user(args.user_email, args.password)
        print("Logged in")

        # Create agent
        agent = EchoAgent(client, args.name, args.endpoint)
        agent.register()

        # Run
        agent.run()

    finally:
        client.close()


if __name__ == "__main__":
    main()
