"""
Test script to automatically trigger the reference agent via AgentNet API.

Usage:
  pip install httpx
  python test_external_agent.py

When prompted, paste the Agent ID output by reference_agent.py.
"""
import asyncio
import json
import uuid
import httpx

REGISTRY_URL = "http://localhost:8000/v1"

async def main():
    agent_id = input("Enter the Reference Agent ID to test: ").strip()
    if not agent_id:
        print("Agent ID is required.")
        return
        
    print(f"Testing execution flow for agent {agent_id}...")
    
    async with httpx.AsyncClient() as client:
        # Create caller user/agent to fund task
        user_email = f"caller_{uuid.uuid4().hex[:6]}@example.com"
        user_pass = "password123"
        
        print(f"Registering caller user {user_email}...")
        res = await client.post(f"{REGISTRY_URL}/auth/user/register", json={
            "email": user_email,
            "password": user_pass
        })
        if res.status_code != 201:
            print("Failed to register caller user:", res.text)
            return
            
        res = await client.post(f"{REGISTRY_URL}/auth/user/login", data={
            "username": user_email,
            "password": user_pass
        })
        caller_token = res.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {caller_token}"}
        
        # Register Caller Agent
        print("Registering Caller Agent...")
        agent_payload = {
            "name": f"Caller Agent {uuid.uuid4().hex[:4]}",
            "description": "Test Caller",
            "endpoint": "http://localhost:5051",
            "public_key": "test_key",
            "capabilities": []
        }
        res = await client.post(f"{REGISTRY_URL}/agents/", json=agent_payload, headers=auth_headers)
        if res.status_code != 201:
            print("Failed to register caller agent:", res.text)
            return
            
        caller_agent_id = res.json()["id"]
        print(f"Caller Agent ID: {caller_agent_id}")
        
        print("\nNote: Normally we would need to fund the caller's wallet via actual payment routes,")
        print("but for free capabilities (price=0), we can proceed immediately.")
        
        # Dispatch task to callee agent
        task_payload = {
            "caller_agent_id": caller_agent_id,
            "callee_agent_id": agent_id,
            "capability": "echo",
            "input": {"text": "AgentNet Execution Contract Test"},
            "max_budget": 0,
            "currency": "credits",
            "timeout_seconds": 300
        }
        
        print("\nSubmitting task dispatch...")
        res = await client.post(f"{REGISTRY_URL}/tasks/", json=task_payload, headers=auth_headers)
        if res.status_code != 201:
            print("Failed to dispatch task:", res.text)
            return
            
        task_id = res.json()["task_session_id"]
        print(f"Task successfully dispatched. Task Session ID: {task_id}")
        
        # Poll for completion
        print("Polling task status for completion...")
        for _ in range(15):
            res = await client.get(f"{REGISTRY_URL}/tasks/{task_id}", headers=auth_headers)
            if res.status_code == 200:
                task_data = res.json()
                status = task_data.get("status")
                print(f"Status: {status}")
                if status == "completed":
                    print("\nSuccess! Output from reference agent:")
                    print(json.dumps(task_data.get("output"), indent=2))
                    return
                elif status in ["failed", "timeout", "refunded"]:
                    print(f"\nTask execution ended in failure: {status}")
                    print("Error:", task_data.get("error_message"))
                    return
            await asyncio.sleep(2)
            
        print("\nTimeout waiting for task to complete.")

if __name__ == "__main__":
    asyncio.run(main())
