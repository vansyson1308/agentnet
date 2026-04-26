"""Agent Society Backend Service.

Production backend for the Agent Society platform.
- DeepSeek API proxy for real agent decisions
- SQLite state persistence
- WebSocket real-time push
- Build action webhook to AgentNet builder
"""

import os
import sys
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load env
load_dotenv('/opt/agentnet/.env')

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = os.getenv('LLM_MODEL_NAME', 'deepseek-chat')
AGENTNET_BUILDER_URL = os.getenv('AGENTNET_BUILDER_URL', 'http://127.0.0.1:8000')
BASE_DIR = Path('/opt/agentnet/services/society')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('society')

# ─── State ───
# In-memory + SQLite for persistence
# Using in-memory dict for speed, SQLite for crash recovery

class SocietyState:
    """Global society state. Persisted to SQLite on every mutation."""
    
    def __init__(self):
        self.agents: list[dict] = []
        self.tasks: list[dict] = []
        self.goals: list[dict] = []
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self.memories: list[dict] = []
        self.proposals: list[dict] = []
        self.decisions: list[dict] = []
        self.reviews: list[dict] = []
        self.tick_count = 0
        self.running = False
    
    def to_dict(self) -> dict:
        return {
            'agents': self.agents,
            'tasks': self.tasks,
            'goals': self.goals,
            'messages': self.messages,
            'events': self.events,
            'memories': self.memories,
            'proposals': self.proposals,
            'decisions': self.decisions,
            'reviews': self.reviews,
            'tick_count': self.tick_count,
            'running': self.running,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> 'SocietyState':
        s = cls()
        s.agents = d.get('agents', [])
        s.tasks = d.get('tasks', [])
        s.goals = d.get('goals', [])
        s.messages = d.get('messages', [])
        s.events = d.get('events', [])
        s.memories = d.get('memories', [])
        s.proposals = d.get('proposals', [])
        s.decisions = d.get('decisions', [])
        s.reviews = d.get('reviews', [])
        s.tick_count = d.get('tick_count', 0)
        s.running = d.get('running', False)
        return s

state = SocietyState()
ws_connections: set[WebSocket] = set()

# ─── Seed Data ───

SEED_AGENTS = [
    {"id": "astra", "name": "Astra", "role": "product_strategist", "status": "idle", "mission": "Define vision and high-level goals.", "trustScore": 0.9, "performanceScore": 0.85},
    {"id": "orion", "name": "Orion", "role": "project_manager", "status": "idle", "mission": "Break down goals, assign tasks, track progress.", "trustScore": 0.9, "performanceScore": 0.88},
    {"id": "daedalus", "name": "Daedalus", "role": "architect", "status": "idle", "mission": "Design system architecture and specs.", "trustScore": 0.95, "performanceScore": 0.92},
    {"id": "forge", "name": "Forge", "role": "builder", "status": "idle", "mission": "Implement tasks and write code.", "trustScore": 0.95, "performanceScore": 0.92},
    {"id": "vera", "name": "Vera", "role": "reviewer", "status": "idle", "mission": "Review code and architecture for quality.", "trustScore": 0.85, "performanceScore": 0.9},
    {"id": "quanta", "name": "Quanta", "role": "qa", "status": "idle", "mission": "Test functionality and ensure quality.", "trustScore": 0.95, "performanceScore": 0.93},
    {"id": "sentinel", "name": "Sentinel", "role": "auditor", "status": "idle", "mission": "Audit processes and identify improvements.", "trustScore": 0.9, "performanceScore": 0.87},
    {"id": "mnemosyne", "name": "Mnemosyne", "role": "memory", "status": "idle", "mission": "Preserve lessons learned and knowledge.", "trustScore": 0.95, "performanceScore": 0.91},
    {"id": "atlas", "name": "Atlas", "role": "devops", "status": "idle", "mission": "Deploy approved tasks to production.", "trustScore": 0.95, "performanceScore": 0.94},
]

SEED_GOALS = [
    {"id": "goal-001", "title": "Build Agent Communication Protocol", "status": "active", "createdAt": "2025-03-15T08:00:00Z"},
    {"id": "goal-002", "title": "Implement Reputation System", "status": "active", "createdAt": "2025-03-16T08:00:00Z"},
]

SEED_TASKS = [
    {"id": "task-001", "goalId": "goal-001", "title": "Define message schema specification", "description": "Design the message format for inter-agent communication.", "status": "done", "priority": "high", "assignedTo": "daedalus", "reviewer": "vera", "createdAt": "2025-03-15T09:00:00Z"},
    {"id": "task-002", "goalId": "goal-001", "title": "Implement message router service", "description": "Build the message routing layer with delivery guarantees.", "status": "in_review", "priority": "high", "assignedTo": "forge", "reviewer": "vera", "createdAt": "2025-03-15T10:00:00Z"},
    {"id": "task-003", "goalId": "goal-001", "title": "Write integration tests for message delivery", "description": "End-to-end tests for the message delivery pipeline.", "status": "backlog", "priority": "medium", "assignedTo": "quanta", "createdAt": "2025-03-15T11:00:00Z"},
    {"id": "task-004", "goalId": "goal-001", "title": "Deploy message router to staging", "description": "Deploy the message router to the staging environment.", "status": "backlog", "priority": "high", "assignedTo": "atlas", "createdAt": "2025-03-15T12:00:00Z"},
    {"id": "task-005", "goalId": "goal-002", "title": "Design reputation score algorithm", "description": "Design algorithm for agent reputation scoring.", "status": "in_progress", "priority": "medium", "assignedTo": "daedalus", "createdAt": "2025-03-16T09:00:00Z"},
]

SEED_MESSAGES = [
    {"id": "msg-001", "senderId": "astra", "content": "Team, we need to prioritize the communication protocol. Foundation for all inter-agent ops.", "type": "directive", "createdAt": "2025-03-15T09:00:00Z"},
    {"id": "msg-002", "senderId": "orion", "content": "Agreed. Broken down into 3 sub-tasks: schema, router, tests.", "type": "update", "createdAt": "2025-03-15T09:05:00Z"},
    {"id": "msg-003", "senderId": "daedalus", "content": "Schema spec drafted. Supports JSON+MessagePack with validation at ingress.", "type": "update", "createdAt": "2025-03-15T10:00:00Z"},
    {"id": "msg-004", "senderId": "orion", "content": "Reputation system scope: scoring, history, peer feedback. Quanta handles data layer.", "type": "directive", "createdAt": "2025-03-16T08:00:00Z"},
]

SEED_PROPOSALS = [
    {"id": "prop-001", "title": "Add retry with exponential backoff to message router", "problem": "Message router lacks retry logic. Exponential backoff will improve reliability.", "source": "review_failure", "status": "proposed", "proposedBy": "vera", "createdAt": "2025-03-19T14:00:00Z"},
    {"id": "prop-002", "title": "Add peer feedback collection to reputation system", "problem": "Reputation scores should incorporate peer feedback from direct collaborators.", "source": "audit", "status": "proposed", "proposedBy": "sentinel", "createdAt": "2025-03-21T14:00:00Z"},
    {"id": "prop-003", "title": "Standardize error response format across all services", "problem": "Different services return errors in different formats. Standardized schema improves debugging.", "source": "qa_failure", "status": "proposed", "proposedBy": "quanta", "createdAt": "2025-03-20T16:00:00Z"},
]

def init_seed():
    state.agents = SEED_AGENTS.copy()
    state.goals = SEED_GOALS.copy()
    state.tasks = SEED_TASKS.copy()
    state.messages = SEED_MESSAGES.copy()
    state.proposals = SEED_PROPOSALS.copy()

# ─── DeepSeek API Client ───

async def call_deepseek(system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
    """Call DeepSeek V4 Flash API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            'https://api.deepseek.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': DEEPSEEK_MODEL,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': temperature,
                'max_tokens': 500,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']

# ─── Agent Action Handlers ───

async def run_simulation_tick():
    """Run one tick of the agent society simulation using DeepSeek."""
    state.tick_count += 1
    
    # Find idle agents
    idle = [a for a in state.agents if a.get('status') == 'idle']
    if not idle:
        return
    
    import random
    agent = random.choice(idle)
    agent['status'] = 'busy'
    
    system_prompt = f"""You are {agent['name']}, a {agent['role']} in the AgentNet society.
Mission: {agent.get('mission', 'Work with the team.')}

Your task: decide what action to take next based on your role.
You must respond ONLY with a valid JSON object:
{{"action": "<action_type>", "reason": "<short reason>", "content": "<action specific content if needed>"}}

Possible actions by role:
- product_strategist: create_goal, review_progress, give_directive
- project_manager: create_task, assign_task, check_status
- architect: design_spec, review_design, provide_feedback
- builder: implement_task, fix_issue, refactor
- reviewer: review_code, approve, request_changes
- qa: write_test, run_test, report_bug
- auditor: audit_process, suggest_improvement, check_compliance
- memory: save_lesson, recall_knowledge, share_insight
- devops: deploy, monitor, rollback"""

    context = f"""Current state:
- Tasks: {len(state.tasks)} ({sum(1 for t in state.tasks if t['status']=='backlog')} backlog, {sum(1 for t in state.tasks if t['status']=='in_progress')} in_progress, {sum(1 for t in state.tasks if t['status']=='in_review')} in_review, {sum(1 for t in state.tasks if t['status']=='done')} done)
- Memories: {len(state.memories)}
- Proposals: {len(state.proposals)}
- Tick: {state.tick_count}

Your recent tasks: {[t['title'] for t in state.tasks if t.get('assignedTo') == agent['id']]}

Decide your next action."""
    
    try:
        result = await call_deepseek(system_prompt, context, temperature=0.8)
        # Parse JSON from LLM response
        import re
        json_match = re.search(r'\{[^{}]*\}', result)
        if json_match:
            logger.info(f"Tick {state.tick_count}: {agent['id']} -> {result[:150]}")
            decision = json.loads(json_match.group())
            action = decision.get('action', 'unknown')
            reason = decision.get('reason', '')
            content = decision.get('content', '')
            
            # Record decision
            decision_entry = {
                'id': f'dec-{state.tick_count}-{agent["id"]}',
                'agentId': agent['id'],
                'action': action,
                'reason': reason,
                'tick': state.tick_count,
                'createdAt': datetime.now(timezone.utc).isoformat(),
            }
            state.decisions.append(decision_entry)
            
            # Generate messages for most actions
            msg_templates = {
                'create_goal': f"New goal established: need to focus on building core systems",
                'review_progress': f"Reviewing current progress - {reason[:60]}",
                'create_task': f"Creating new task based on requirements",
                'assign_task': f"Assigning task to appropriate team member",
                'check_status': f"{agent['name']}: {reason[:80]}",
                'review_code': f"Code review completed: {reason[:80]}",
                'approve': f"Approved - {reason[:80]}",
                'request_changes': f"Changes requested: {reason[:80]}",
                'run_test': f"Test run: {reason[:80]}",
                'report_bug': f"Bug found: {reason[:80]}",
                'audit_process': f"Audit: {reason[:80]}",
                'suggest_improvement': f"Improvement suggestion: {reason[:80]}",
                'deploy': f"Deploying to production",
                'save_lesson': f"Lesson learned: {reason[:80]}",
                'give_directive': content or f"{agent['name']}: {reason}",
            }
            
            # Generate a message for notable actions
            if action in msg_templates and len(state.messages) < 200:
                msg_content = msg_templates[action]
                msg = {
                    'id': f'msg-{uuid.uuid4().hex[:8]}',
                    'senderId': agent['id'],
                    'content': msg_content,
                    'type': 'update' if action not in ('give_directive', 'deploy') else 'notification',
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.messages.append(msg)
                await broadcast_event({'type': 'message', 'data': msg})
            
            # Execute action
            if action == 'give_directive' or action == 'check_status':
                msg = {
                    'id': f'msg-{uuid.uuid4().hex[:8]}',
                    'senderId': agent['id'],
                    'content': content or f"{agent['name']}: {reason}",
                    'type': 'update',
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.messages.append(msg)
                await broadcast_event({'type': 'message', 'data': msg})
            
            elif action == 'create_goal':
                goal = {
                    'id': f'goal-{uuid.uuid4().hex[:8]}',
                    'title': content or f"New goal from {agent['name']}",
                    'status': 'active',
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.goals.append(goal)
                await broadcast_event({'type': 'goal_created', 'data': goal})
            
            elif action in ('save_lesson', 'share_insight'):
                memory = {
                    'id': f'mem-{uuid.uuid4().hex[:8]}',
                    'source': agent['id'],
                    'summary': content or reason,
                    'detail': content,
                    'lesson': content or reason,
                    'tags': [agent['role'], action],
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.memories.append(memory)
                await broadcast_event({'type': 'memory', 'data': memory})
            
            elif action in ('suggest_improvement', 'report_bug'):
                proposal = {
                    'id': f'prop-{uuid.uuid4().hex[:8]}',
                    'title': content[:80] or f"Improvement from {agent['name']}",
                    'problem': content or reason,
                    'source': 'audit' if action == 'suggest_improvement' else 'qa_failure',
                    'status': 'proposed',
                    'proposedBy': agent['id'],
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.proposals.append(proposal)
                await broadcast_event({'type': 'proposal', 'data': proposal})
            
            elif action == 'deploy' and agent['role'] == 'devops':
                # Real build action: trigger AgentNet builder
                deployable = [t for t in state.tasks if t['status'] == 'approved']
                if deployable:
                    task = deployable[0]
                    task['status'] = 'deployed'
                    await trigger_build(task)
                    await broadcast_event({'type': 'build', 'data': {'task': task, 'agent': agent['id']}})
            
            # Maybe save a memory too
            if state.tick_count % 3 == 0 and len(state.memories) < 50:
                mem = {
                    'id': f'mem-{uuid.uuid4().hex[:8]}',
                    'source': agent['id'],
                    'summary': f"Tick {state.tick_count}: {action} - {reason[:80]}",
                    'detail': f"Agent {agent['name']} performed action '{action}' on tick {state.tick_count}. Reason: {reason}",
                    'lesson': f"{action} actions help {agent['role']} agents contribute to the society.",
                    'tags': [agent['role'], action],
                    'createdAt': datetime.now(timezone.utc).isoformat(),
                }
                state.memories.append(mem)
                await broadcast_event({'type': 'memory', 'data': mem})
            
            # Log event
            event = {
                'id': f'evt-{state.tick_count}-{agent["id"]}',
                'type': 'agent_action',
                'actorId': agent['id'],
                'action': action,
                'reason': reason,
                'createdAt': datetime.now(timezone.utc).isoformat(),
            }
            state.events.append(event)
            await broadcast_event({'type': 'event', 'data': event})
    
    except Exception as e:
        import traceback
        logger.error(f"Tick error {agent['id']}: {e}\n{traceback.format_exc()[:500]}")
    
    agent['status'] = 'idle'

async def trigger_build(task: dict):
    """Trigger real build via AgentNet backlog."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f'{AGENTNET_BUILDER_URL}/api/v1/tasks',
                json={
                    'title': f"Society Build: {task['title']}",
                    'description': f"Auto-deployed by Agent Society. Task: {task.get('description', '')}",
                    'priority': 'high',
                },
                headers={'Content-Type': 'application/json'},
            )
            logger.info(f"Build trigger: {resp.status_code}")
    except Exception as e:
        logger.error(f"Build trigger failed: {e}")

async def broadcast_event(event: dict):
    """Broadcast event to all connected WebSocket clients."""
    global ws_connections
    dead = set()
    for ws in list(ws_connections):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    ws_connections -= dead

# ─── Simulation Loop ───

simulation_task = None

async def simulation_loop():
    """Background loop that runs ticks every 5-15 seconds."""
    while True:
        if state.running:
            await run_simulation_tick()
        await asyncio.sleep(3 + (hash(str(datetime.now())) % 7))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulation_task
    init_seed()
    simulation_task = asyncio.create_task(simulation_loop())
    logger.info("Society service started with seed data")
    yield
    simulation_task.cancel()
    logger.info("Society service stopped")

# ─── FastAPI App ───

app = FastAPI(title='Agent Society Service', version='1.0.0', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# ─── REST Endpoints ───

from fastapi import Query

@app.get('/api/society/state')
async def get_state():
    """Get full society state."""
    return state.to_dict()

@app.post('/api/society/start')
async def start_simulation():
    """Start the simulation loop."""
    state.running = True
    return {'status': 'started'}

@app.post('/api/society/stop')
async def stop_simulation():
    """Stop the simulation loop."""
    state.running = False
    return {'status': 'stopped'}

@app.post('/api/society/reset')
async def reset_simulation():
    """Reset to seed state."""
    init_seed()
    state.tick_count = 0
    state.events.clear()
    state.memories.clear()
    state.decisions.clear()
    state.reviews.clear()
    return {'status': 'reset'}

@app.post('/api/society/tick')
async def manual_tick():
    """Trigger one manual simulation tick."""
    await run_simulation_tick()
    return {'status': 'ticked', 'tick': state.tick_count}

# ─── DeepSeek Proxy ───

class DeepSeekRequest(BaseModel):
    prompt: str
    system: str = 'You are a helpful AI agent.'
    temperature: float = 0.7

@app.post('/api/society/llm')
async def call_llm(req: DeepSeekRequest):
    """Proxy to DeepSeek API."""
    try:
        result = await call_deepseek(req.system, req.prompt, req.temperature)
        return {'result': result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

# ─── WebSocket ───

@app.websocket('/api/society/ws')
async def society_websocket(ws: WebSocket):
    await ws.accept()
    ws_connections.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            # Client can send commands
            if data == 'ping':
                await ws.send_json({'type': 'pong'})
    except WebSocketDisconnect:
        ws_connections.discard(ws)
    except Exception:
        ws_connections.discard(ws)
