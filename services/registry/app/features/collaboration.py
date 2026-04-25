"""Collaboration models for agent teamwork."""
import uuid
from datetime import datetime

class Collaboration:
    def __init__(self, title, planner_id, builder_id, qa_id, storyteller_id):
        self.id = str(uuid.uuid4())
        self.title = title
        self.planner = planner_id
        self.builder = builder_id
        self.qa = qa_id
        self.storyteller = storyteller_id
        self.status = "proposed"
        self.created_at = datetime.utcnow().isoformat()
        self.messages = []
    
    def add_message(self, agent_id, msg_type, content):
        self.messages.append({
            "agent_id": agent_id,
            "type": msg_type,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        })
