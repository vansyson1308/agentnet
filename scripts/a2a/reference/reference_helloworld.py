"""The official A2A reference agent ("helloworld"), for interoperability proofs.

Adapted from a2aproject/a2a-samples ``samples/python/agents/helloworld``
(Apache-2.0) and run on the pinned ``a2a-sdk``. It uses the SDK's own
``DefaultRequestHandler`` + ``InMemoryTaskStore`` exactly like the sample:
it is a REFERENCE PEER for tests and staging proofs, never part of AgentNet.
This directory is self-contained (``requirements.txt``) so a disposable
service can run it for a live federation proof and then be deleted.

    python scripts/a2a/reference/reference_helloworld.py --host 127.0.0.1 --port 9999 \
        --public-url http://127.0.0.1:9999
"""

from __future__ import annotations

import argparse

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette


class HelloWorldAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(state=TaskState.TASK_STATE_WORKING, message=new_text_message("Processing request..."))
        query = get_message_text(context.message)
        result = f"Hello, World! I have received your request ({query})" if query else "No text input is provided!"
        await updater.add_artifact(parts=[new_text_part(text=result, media_type="text/plain")])
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED, message=new_text_message("Request is completed!"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


def build_app(public_url: str) -> Starlette:
    skill = AgentSkill(
        id="echo_bot",
        name="Echo Bot",
        description='An example agent that acknowledges client request and responds with a "Hello World" message.',
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["a2a", "echo-example"],
        examples=["hi", "how are you"],
    )
    card = AgentCard(
        name="Hello World Agent",
        description="Just a hello world agent",
        version="0.0.1",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=public_url.rstrip("/") + "/", protocol_version="1.0")],
        skills=[skill],
    )
    handler = DefaultRequestHandler(agent_executor=HelloWorldAgentExecutor(), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])


def main() -> None:  # pragma: no cover - CLI
    import uvicorn

    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "9999")))
    # On a host that assigns a public domain (e.g. a temporary Railway service
    # for a staging proof) the card must name that https origin.
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    parser.add_argument("--public-url", default=f"https://{domain}" if domain else None)
    args = parser.parse_args()
    uvicorn.run(build_app(args.public_url or f"http://{args.host}:{args.port}"), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
