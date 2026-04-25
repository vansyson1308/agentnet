"""Chat Threads: Agent-to-agent conversation with thread_id support."""
import uuid
from datetime import datetime

def create_chat_thread(from_agent, to_agent, title):
    return {
        "thread_id": str(uuid.uuid4()),
        "from_agent": from_agent,
        "to_agent": to_agent,
        "title": title,
        "created_at": datetime.utcnow().isoformat(),
        "messages": []
    }
