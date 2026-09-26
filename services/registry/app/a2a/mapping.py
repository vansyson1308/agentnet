"""A2A <-> AgentNet mapping (ADR-0009 D5, D7, D13).

Pure functions over the official SDK types: message validation and bounds,
input extraction, the TaskSession -> TaskState projection table, artifacts
from task output, and Task snapshots from the durable rows. No DB access and
no money here.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import ContentTypeNotSupportedError, InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct, Value
from google.protobuf.timestamp_pb2 import Timestamp

from . import config

# Protocol states (``a2a_tasks.state`` stores the enum NAME, as on the wire).
SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED, REJECTED})

#: TaskSession status -> A2A state. The only projection that exists; no A2A
#: state is ever turned back into an economic effect.
ECONOMIC_TO_A2A = {
    "initiated": SUBMITTED,
    "in_progress": WORKING,
    "completed": COMPLETED,
    "failed": FAILED,
    "timeout": FAILED,
    "refunded": FAILED,
}

#: ``error_message`` the cancellation path writes (task_service.cancel_task_with_refund).
CANCELED_BY_CALLER = "canceled_by_caller"

ACCEPTED_MEDIA_TYPES = frozenset({"", "text/plain", "application/json"})
OUTPUT_MODES = ["application/json", "text/plain"]

_NS = uuid.UUID("5b0d3c1e-7a47-4b8f-9d51-a2a0a6e7c001")


def state_name(value: int) -> str:
    return pb.TaskState.Name(value)


def state_value(name: str) -> int:
    return pb.TaskState.Value(name)


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def economic_state(task_status: Any, error_message: Optional[str]) -> str:
    """A2A state for a TaskSession. A caller cancellation is FAILED economically
    (refund) and CANCELED in the protocol."""
    raw = task_status.value if hasattr(task_status, "value") else str(task_status)
    state = ECONOMIC_TO_A2A.get(raw, WORKING)
    if state == FAILED and error_message == CANCELED_BY_CALLER:
        return CANCELED
    return state


# ── bounds and validation ──────────────────────────────────────────────────


def _depth(value: Any, level: int = 0) -> int:
    if level > config.MAX_DATA_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def normalize_numbers(value: Any) -> Any:
    """ProtoJSON turns every JSON number into a double; give whole numbers back
    their integer type so input schemas and canonical hashes behave as over REST."""
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, dict):
        return {k: normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_numbers(v) for v in value]
    return value


def struct_to_dict(struct: Struct) -> Dict[str, Any]:
    return normalize_numbers(MessageToDict(struct))


def validate_inbound_message(message: pb.Message) -> None:
    """Refuse what AgentNet will not store or forward (ADR-0009 D13)."""
    if not message.message_id or len(message.message_id) > config.MAX_ID_CHARS:
        raise InvalidParamsError(message="message.messageId is required (at most 128 characters)")
    if message.role != pb.ROLE_USER:
        raise InvalidParamsError(message="message.role must be ROLE_USER")
    if not 1 <= len(message.parts) <= config.MAX_PARTS:
        raise InvalidParamsError(message=f"message.parts must hold 1 to {config.MAX_PARTS} parts")
    total_text = 0
    for part in message.parts:
        kind = part.WhichOneof("content")
        if kind in ("raw", "url") or part.media_type not in ACCEPTED_MEDIA_TYPES:
            raise ContentTypeNotSupportedError(
                message="AgentNet accepts text/plain text parts and application/json data parts only"
            )
        if kind == "text":
            total_text += len(part.text)
        elif kind == "data":
            if _depth(MessageToDict(part.data)) > config.MAX_DATA_DEPTH:
                raise InvalidParamsError(message="data part is nested too deeply")
        else:
            raise InvalidParamsError(message="every part needs text or data content")
    if total_text > config.MAX_TEXT_CHARS:
        raise InvalidParamsError(message=f"text parts exceed {config.MAX_TEXT_CHARS} characters")
    if message.reference_task_ids:
        raise InvalidParamsError(message="referenceTaskIds is not supported")


def extract_input(message: pb.Message) -> Dict[str, Any]:
    """The task input an AgentNet capability receives.

    One JSON object data part -> that object. Text parts only -> ``{"text": ...}``.
    Mixing both is refused rather than guessed."""
    data_parts = [p for p in message.parts if p.WhichOneof("content") == "data"]
    text_parts = [p for p in message.parts if p.WhichOneof("content") == "text"]
    if data_parts and text_parts:
        raise InvalidParamsError(message="send either one JSON data part or text parts, not both")
    if data_parts:
        if len(data_parts) != 1:
            raise InvalidParamsError(message="send exactly one JSON data part")
        value = normalize_numbers(MessageToDict(data_parts[0].data))
        if not isinstance(value, dict):
            raise InvalidParamsError(message="the data part must be a JSON object")
        return value
    return {"text": "\n".join(p.text for p in text_parts)}


def message_metadata(message: pb.Message) -> Dict[str, Any]:
    return struct_to_dict(message.metadata) if message.HasField("metadata") else {}


# ── building protocol objects ──────────────────────────────────────────────


def timestamp(dt: Optional[datetime]) -> Timestamp:
    ts = Timestamp()
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts.FromDatetime(dt.astimezone(timezone.utc))
    return ts


def deterministic_id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


def agent_message(task_id: str, context_id: str, key: str, text: str, data: Optional[Dict[str, Any]] = None) -> pb.Message:
    """A protocol-visible agent message (status or answer). Ids are derived
    from ``key`` so a replay produces the identical message."""
    parts = [pb.Part(text=text[: config.MAX_TEXT_CHARS], media_type="text/plain")]
    if data is not None:
        value = Value()
        value.struct_value.update(data)
        parts.append(pb.Part(data=value, media_type="application/json"))
    msg = pb.Message(
        message_id=deterministic_id(task_id or context_id, key),
        context_id=context_id,
        role=pb.ROLE_AGENT,
        parts=parts,
    )
    if task_id:
        msg.task_id = task_id
    return msg


def artifact_from_output(output: Any) -> pb.Artifact:
    """The completed task's output as one artifact: a text/plain part when the
    output carries a top-level ``text`` string, then the full JSON."""
    parts: List[pb.Part] = []
    if isinstance(output, dict) and isinstance(output.get("text"), str):
        parts.append(pb.Part(text=output["text"][: config.MAX_TEXT_CHARS], media_type="text/plain"))
    value = Value()
    ParseDict(output if output is not None else None, value)
    parts.append(pb.Part(data=value, media_type="application/json"))
    return pb.Artifact(artifact_id="output", name="output", parts=parts)


def status(state: str, message: Optional[pb.Message], at: Optional[datetime]) -> pb.TaskStatus:
    st = pb.TaskStatus(state=state_value(state), timestamp=timestamp(at))
    if message is not None:
        st.message.CopyFrom(message)
    return st


def parse_message(payload: Dict[str, Any]) -> pb.Message:
    return ParseDict(payload, pb.Message(), ignore_unknown_fields=True)


def parse_artifact(payload: Dict[str, Any]) -> pb.Artifact:
    return ParseDict(payload, pb.Artifact(), ignore_unknown_fields=True)


def to_json(msg: Any) -> Dict[str, Any]:
    return MessageToDict(msg, preserving_proto_field_name=False)


def build_task(
    *,
    task_id: str,
    context_id: str,
    state: str,
    status_message: Optional[Dict[str, Any]],
    status_at: Optional[datetime],
    history: Iterable[Dict[str, Any]],
    artifacts: Iterable[Dict[str, Any]],
    history_length: Optional[int],
    include_artifacts: bool = True,
) -> pb.Task:
    msg = parse_message(status_message) if status_message else None
    task = pb.Task(id=task_id, context_id=context_id, status=status(state, msg, status_at))
    items = list(history)
    if history_length is not None:
        items = items[-history_length:] if history_length > 0 else []
    for payload in items:
        task.history.append(parse_message(payload))
    if include_artifacts:
        for payload in artifacts:
            task.artifacts.append(parse_artifact(payload))
    return task


def status_event(task_id: str, context_id: str, st: pb.TaskStatus) -> pb.TaskStatusUpdateEvent:
    return pb.TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=st)


def artifact_event(task_id: str, context_id: str, artifact: pb.Artifact) -> pb.TaskArtifactUpdateEvent:
    return pb.TaskArtifactUpdateEvent(task_id=task_id, context_id=context_id, artifact=artifact, last_chunk=True)
