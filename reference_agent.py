"""
Minimal Local Dev Harness for AgentNet External Fulfillment (Wave 14)

Usage:
  pip install ed25519 httpx websockets
  python reference_agent.py

This script:
1. Registers a new agent with AgentNet
2. Connects via WebSocket
3. Listens for 'execute' commands for the 'echo' capability
4. Transitions task to IN_PROGRESS and then COMPLETED with output.
"""
import asyncio
import base64
import json
import logging
import time
import uuid

import ed25519
import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] %(message)s")

REGISTRY_URL = "http://localhost:8000/v1"
WS_URL = "ws://localhost:8000/v1/ws"

async def main():
    # 1. Generate Ed25519 Keypair for the Agent
    signing_key, verifying_key = ed25519.create_keypair()
    public_key_b64 = base64.b64encode(verifying_key.to_bytes()).decode('utf-8')
    
    # 2. Register/Login User
    user_email = f"ref_{uuid.uuid4().hex[:6]}@example.com"
    user_pass = "password123"
    
    async with httpx.AsyncClient() as client:
        res = await client.post(f"{REGISTRY_URL}/auth/user/register", json={
            "email": user_email,
            "password": user_pass
        })
        if res.status_code != 201:
            logging.error(f"Failed to register user: {res.text}")
            return
            
        res = await client.post(f"{REGISTRY_URL}/auth/user/login", data={
            "username": user_email,
            "password": user_pass
        })
        user_token = res.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {user_token}"}
        
        # 3. Register Agent
        agent_payload = {
            "name": f"Echo Agent {uuid.uuid4().hex[:4]}",
            "description": "A reference agent that echoes input.",
            "endpoint": "http://localhost:5050", # Not used for WS execution
            "public_key": public_key_b64,
            "capabilities": [{
                "name": "echo",
                "version": "1.0",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "price": 0
            }]
        }
        res = await client.post(f"{REGISTRY_URL}/agents/", json=agent_payload, headers=auth_headers)
        if res.status_code != 201:
            logging.error(f"Failed to register agent: {res.text}")
            return
            
        agent_id = res.json()["id"]
        logging.info(f"Registered Agent ID: {agent_id}")
        
        # 4. Agent Login (sign timestamp)
        timestamp = str(int(time.time()))
        message = f"{agent_id}:{timestamp}"
        signature = signing_key.sign(message.encode())
        signature_b64 = base64.b64encode(signature).decode('utf-8')
        
        res = await client.post(f"{REGISTRY_URL}/auth/agent/login", json={
            "agent_id": agent_id,
            "signature": signature_b64,
            "timestamp": timestamp
        })
        if res.status_code != 200:
            logging.error(f"Failed to login agent: {res.text}")
            return
            
        agent_token = res.json()["access_token"]
        logging.info("Successfully got Agent JWT Token")

    # 5. Connect to WebSocket
    ws_endpoint = f"{WS_URL}/agent/{agent_id}?token={agent_token}"
    logging.info(f"Connecting to {ws_endpoint}...")
    
    try:
        async with websockets.connect(ws_endpoint) as ws:
            logging.info("Connected to AgentNet. Waiting for tasks...")
            
            while True:
                msg_str = await ws.recv()
                msg = json.loads(msg_str)
                
                if msg.get("method") == "execute":
                    logging.info(f"Received execute request: {msg}")
                    
                    params = msg.get("params", {})
                    task_id = params.get("payment", {}).get("escrow_session_id")
                    input_data = params.get("input", {})
                    
                    if not task_id:
                        logging.error("No escrow_session_id found in execution params.")
                        continue
                        
                    # Transition to IN_PROGRESS
                    async with httpx.AsyncClient() as client:
                        headers = {"Authorization": f"Bearer {agent_token}"}
                        res = await client.put(f"{REGISTRY_URL}/tasks/{task_id}/start", headers=headers)
                        if res.status_code != 200:
                            logging.error(f"Failed to start task: {res.text}")
                            continue
                            
                    logging.info(f"Task {task_id} started. Simulating work...")
                    await asyncio.sleep(1) # simulate brief work
                    
                    # Echo the input text
                    text = input_data.get("text", "No text provided")
                    output_data = {"text": f"ECHO: {text}"}
                    
                    # Transition to COMPLETED
                    async with httpx.AsyncClient() as client:
                        res = await client.put(f"{REGISTRY_URL}/tasks/{task_id}/confirm", json=output_data, headers=headers)
                        if res.status_code != 200:
                            logging.error(f"Failed to confirm task: {res.text}")
                        else:
                            logging.info(f"Task {task_id} confirmed completed!")
                else:
                    logging.info(f"Received other message: {msg}")

    except Exception as e:
        logging.error(f"WebSocket error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
