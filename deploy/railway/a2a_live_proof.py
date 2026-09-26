#!/usr/bin/env python3
"""A2A live proof -- runs INSIDE a Railway environment (Phase 8, ADR-0009).

Started as the `staging-validator` service (registry image; VALIDATOR_SCRIPT=
a2a_live_proof.py). It speaks to the PUBLIC registry through Railway's edge
with the OFFICIAL a2a-sdk client, exactly as an outside agent would, and prints
one line per check:

    CHECK <id> PASS|FAIL <detail>
    A2A PROOF RESULT: GREEN | RED <failed ids>

Test identities only: one validator user owns two proof agents whose Ed25519
keys are DERIVED from the validator secret (so re-runs reuse the same agents
and nothing new accumulates), and an allow-listed operator runs the
federation / company steps. No money moves: the proof skills are free, and
the paid path is proven by its refusals (no extension, over-budget,
unfunded wallet). Nothing secret is printed -- JWTs stay in memory.

Modes (A2A_PROOF_MODES, comma separated; default "server"):
    server      cards, auth, version negotiation, search, tasks over both
                bindings, streaming, cancel, list, BOLA, paid-path refusals
    federation  operator catalog: SSRF refusals, discover -> verify ->
                connection -> outbound call -> check (A2A_REFERENCE_CARD_URL)
    company     operator company status + an immediate company cycle (or
                A2A_PROOF_CYCLE_ID, a running one), wait for the Society to
                settle it (A2A_PROOF_CYCLE_TIMEOUT, default 45 min: cycles
                settle after 30 min), then show the role runs, their models
                and the intents the cycle produced
    incident    open an incident freeze, see it in the status, lift it

Environment: REGISTRY_PUBLIC_URL, STAGING_VALIDATOR_SECRET (or
VALIDATOR_SECRET), VALIDATOR_USER_EMAIL, VALIDATOR_OPERATOR_EMAIL,
POSTGRES_* (staging only: marks the validator's own users verified, as
validate_staging.py does), A2A_REFERENCE_CARD_URL.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import pathlib
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import validate_staging as vs  # noqa: E402  (Report, http, ensure_user, derive_password)

ECON = "https://agentnet.io.vn/a2a/extensions/economics/v1"
PROOF_CAPS = [
    {"name": "a2a-proof-echo", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0},
    {"name": "a2a-proof-hold", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0},
    {"name": "a2a-proof-paid", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 5},
]
CALLEE_NAME = "A2A_Proof_Callee"
CALLER_NAME = "A2A_Proof_Caller"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _signing_key(secret: str, label: str):
    import ed25519

    return ed25519.SigningKey(hashlib.sha256(f"agentnet-a2a-proof:{label}:{secret}".encode()).digest())


def _json(body: str) -> Any:
    try:
        return json.loads(body)
    except Exception:
        return None


def ensure_agent(rep: vs.Report, code: str, api: str, user_token: str, secret: str, name: str) -> Optional[str]:
    """Create the proof agent once (deterministic key); reuse it afterwards."""
    pub = base64.b64encode(_signing_key(secret, name).get_verifying_key().to_bytes()).decode()
    st, body = vs.http(
        "POST", f"{api}/v1/agents/", token=user_token,
        body={"name": name, "description": "AgentNet A2A live-proof test agent (no money)", "capabilities": PROOF_CAPS,
              "endpoint": "https://a2a-proof.invalid/agent", "public_key": pub},
    )
    if st == 201:
        agent_id = (_json(body) or {}).get("id")
        rep.record(code, bool(agent_id), f"{name}: created")
        return agent_id
    st2, body2 = vs.http("GET", f"{api}/v1/agents/?capability=a2a-proof-echo&limit=1000", token=user_token)
    for a in _json(body2) or []:
        if a.get("name") == name:
            rep.record(code, True, f"{name}: reused (create said HTTP {st})")
            return a.get("id")
    rep.record(code, False, f"{name}: create HTTP {st}, lookup HTTP {st2}")
    return None


def agent_login(api: str, secret: str, name: str, agent_id: str) -> Optional[str]:
    ts = datetime.now(timezone.utc).isoformat()
    sig = base64.b64encode(_signing_key(secret, name).sign(f"{agent_id}:{ts}".encode())).decode()
    st, body = vs.http("POST", f"{api}/v1/auth/agent/login", body={"agent_id": agent_id, "signature": sig, "timestamp": ts})
    return (_json(body) or {}).get("access_token") if st == 200 else None


class Callee(threading.Thread):
    """The callee's fulfilment loop over the ordinary REST API: every INITIATED
    `a2a-proof-echo` task is started and confirmed; `a2a-proof-hold` is left
    alone so the caller can cancel it."""

    def __init__(self, api: str, token: str) -> None:
        super().__init__(daemon=True)
        self.api, self.token = api, token
        self.stop = threading.Event()
        self.completed: List[str] = []

    def run(self) -> None:
        while not self.stop.is_set():
            st, body = vs.http("GET", f"{self.api}/v1/tasks/?status=initiated&limit=50", token=self.token, timeout=15)
            for t in (_json(body) or []) if st == 200 else []:
                if t.get("capability") != "a2a-proof-echo":
                    continue
                tid = t["id"]
                text = json.dumps(t.get("input") or {}, sort_keys=True)[:200]
                s1, _ = vs.http("PUT", f"{self.api}/v1/tasks/{tid}/start", token=self.token)
                s2, _ = vs.http("PUT", f"{self.api}/v1/tasks/{tid}/confirm", token=self.token, body={"text": f"echo {text}"})
                if s1 == 200 and s2 == 200:
                    self.completed.append(tid)
            self.stop.wait(1.0)


# ── the official SDK client ───────────────────────────────────────────


def _sdk():
    from a2a.client import ClientCallContext, ClientConfig, ClientFactory
    from a2a.client.service_parameters import ServiceParametersFactory, with_a2a_extensions
    from a2a.types import a2a_pb2 as pb

    return ClientCallContext, ClientConfig, ClientFactory, ServiceParametersFactory, with_a2a_extensions, pb


def _message(pb, *, text: Optional[str] = None, data: Optional[dict] = None, metadata: Optional[dict] = None):
    from google.protobuf.struct_pb2 import Struct, Value

    parts = []
    if text is not None:
        parts.append(pb.Part(text=text))
    if data is not None:
        s = Struct()
        s.update(data)
        parts.append(pb.Part(data=Value(struct_value=s)))
    m = pb.Message(message_id=uuid.uuid4().hex, role=pb.ROLE_USER, parts=parts)
    if metadata:
        m.metadata.update(metadata)
    return m


async def _first(client, request, context=None):
    async for event in client.send_message(request, context=context):
        return event
    return None


async def server_proof(rep: vs.Report, api: str, caller_token: str, other_token: str, callee_id: str) -> None:
    import httpx

    ClientCallContext, ClientConfig, ClientFactory, SPF, with_ext, pb = _sdk()
    card_path = f"/v1/agents/{callee_id}/a2a-card"
    for binding, code in (("JSONRPC", "S20"), ("HTTP+JSON", "S30")):
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {caller_token}"}, timeout=90)
        async with http:
            factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False, supported_protocol_bindings=[binding]))
            try:
                network = await factory.create_from_url(api)
                ev = await _first(network, pb.SendMessageRequest(message=_message(pb, text=CALLEE_NAME)))
                data = next((p.data for p in ev.message.parts if p.HasField("data")), None) if ev is not None and ev.HasField("message") else None
                count = int(data.struct_value["count"]) if data is not None else -1
                rep.record(f"{code}a", count >= 1, f"{binding}: network card resolved, marketplace search answered with a Message (matches={count})")
            except Exception as exc:
                rep.record(f"{code}a", False, f"{binding}: network search {type(exc).__name__}: {str(exc)[:120]}")
            try:
                client = await factory.create_from_url(api, relative_card_path=card_path)
                ev = await _first(client, pb.SendMessageRequest(
                    message=_message(pb, data={"binding": binding, "n": 1}, metadata={"skillId": "a2a-proof-echo"}),
                    configuration=pb.SendMessageConfiguration(return_immediately=True)))
                task = ev.task
                rep.record(f"{code}b", task.status.state in (pb.TASK_STATE_SUBMITTED, pb.TASK_STATE_WORKING), f"{binding}: tenant task created ({pb.TaskState.Name(task.status.state)})")
                final = None
                for _ in range(40):
                    final = await client.get_task(pb.GetTaskRequest(id=task.id))
                    if final.status.state == pb.TASK_STATE_COMPLETED:
                        break
                    await asyncio.sleep(1.5)
                art = final.artifacts[0].parts[0].text if final is not None and final.artifacts else ""
                rep.record(f"{code}c", final is not None and final.status.state == pb.TASK_STATE_COMPLETED and art.startswith("echo "),
                           f"{binding}: callee fulfilled over REST -> GetTask COMPLETED with an artifact")
                listed = await client.list_tasks(pb.ListTasksRequest(page_size=50))
                rep.record(f"{code}d", task.id in [t.id for t in listed.tasks], f"{binding}: ListTasks shows the caller's task ({len(listed.tasks)} listed)")
                # cancel before start: the hold skill is never picked up by the callee
                ev = await _first(client, pb.SendMessageRequest(
                    message=_message(pb, data={"hold": True}, metadata={"skillId": "a2a-proof-hold"}),
                    configuration=pb.SendMessageConfiguration(return_immediately=True)))
                canceled = await client.cancel_task(pb.CancelTaskRequest(id=ev.task.id))
                again = await client.cancel_task(pb.CancelTaskRequest(id=ev.task.id))
                rep.record(f"{code}e", canceled.status.state == pb.TASK_STATE_CANCELED and again.status.state == pb.TASK_STATE_CANCELED,
                           f"{binding}: CancelTask before start -> CANCELED, repeat cancel idempotent")
                # paid skill: the core client is refused with nothing created
                before = len((await client.list_tasks(pb.ListTasksRequest(page_size=100))).tasks)
                try:
                    await _first(client, pb.SendMessageRequest(message=_message(pb, text="x", metadata={"skillId": "a2a-proof-paid"})))
                    rep.record(f"{code}f", False, f"{binding}: paid skill without the economics extension was accepted")
                except Exception as exc:
                    after = len((await client.list_tasks(pb.ListTasksRequest(page_size=100))).tasks)
                    rep.record(f"{code}f", "ExtensionSupportRequired" in type(exc).__name__ and after == before,
                               f"{binding}: paid skill without the extension -> {type(exc).__name__}, no task created")
                ctx = ClientCallContext(service_parameters=SPF.create([with_ext([ECON])]))
                ev = await _first(client, pb.SendMessageRequest(
                    message=_message(pb, text="x", metadata={"skillId": "a2a-proof-paid", ECON: {"maxBudget": 1, "currency": "credits"}}),
                    configuration=pb.SendMessageConfiguration(return_immediately=True)), context=ctx)
                rep.record(f"{code}g", ev is not None and ev.task.status.state == pb.TASK_STATE_REJECTED,
                           f"{binding}: maxBudget below the price -> REJECTED before any escrow")
                ev = await _first(client, pb.SendMessageRequest(
                    message=_message(pb, text="x", metadata={"skillId": "a2a-proof-paid", ECON: {"maxBudget": 5, "currency": "credits"}}),
                    configuration=pb.SendMessageConfiguration(return_immediately=True)), context=ctx)
                rep.record(f"{code}h", ev is not None and ev.task.status.state == pb.TASK_STATE_REJECTED,
                           f"{binding}: unfunded caller wallet -> REJECTED (no reservation, no TaskSession)")
            except Exception as exc:
                rep.record(f"{code}x", False, f"{binding}: {type(exc).__name__}: {str(exc)[:160]}")

    # streaming (JSON-RPC SSE): the task, then its updates, to a terminal state
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {caller_token}"}, timeout=120)
    async with http:
        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=True, supported_protocol_bindings=["JSONRPC"]))
        try:
            client = await factory.create_from_url(api, relative_card_path=card_path)
            kinds, states = [], []
            task_id = None
            async for event in client.send_message(pb.SendMessageRequest(message=_message(pb, data={"stream": True}, metadata={"skillId": "a2a-proof-echo"}))):
                kind = event.WhichOneof("payload")
                kinds.append(kind)
                if kind == "task":
                    task_id = event.task.id
                    states.append(event.task.status.state)
                elif kind == "status_update":
                    states.append(event.status_update.status.state)
                if states and states[-1] in (pb.TASK_STATE_COMPLETED, pb.TASK_STATE_FAILED, pb.TASK_STATE_CANCELED, pb.TASK_STATE_REJECTED):
                    break
            rep.record("S40", bool(kinds) and kinds[0] == "task" and states[-1] == pb.TASK_STATE_COMPLETED and "artifact_update" in kinds,
                       f"SSE stream: {kinds[:6]} -> {pb.TaskState.Name(states[-1]) if states else 'none'}")
            # A2A 1.0: subscribing to a terminal task is UnsupportedOperation
            if task_id:
                try:
                    async for _ in client.subscribe(pb.SubscribeToTaskRequest(id=task_id)):
                        break
                    rep.record("S41", False, "SubscribeToTask on a finished task was accepted")
                except Exception as exc:
                    rep.record("S41", "UnsupportedOperation" in type(exc).__name__, f"SubscribeToTask on a finished task -> {type(exc).__name__}")
        except Exception as exc:
            rep.record("S40", False, f"streaming: {type(exc).__name__}: {str(exc)[:160]}")
        # resubscribe to a LIVE task: the snapshot first, then the cancel arrives on the stream
        try:
            client = await factory.create_from_url(api, relative_card_path=card_path)
            ev = await _first(client, pb.SendMessageRequest(
                message=_message(pb, data={"hold": True}, metadata={"skillId": "a2a-proof-hold"}),
                configuration=pb.SendMessageConfiguration(return_immediately=True)))
            held = ev.task.id

            async def follow():
                seen = []
                async for event in client.subscribe(pb.SubscribeToTaskRequest(id=held)):
                    kind = event.WhichOneof("payload")
                    state = event.task.status.state if kind == "task" else event.status_update.status.state if kind == "status_update" else None
                    seen.append((kind, state))
                    if state == pb.TASK_STATE_CANCELED:
                        break
                return seen

            follower = asyncio.create_task(follow())
            await asyncio.sleep(2)
            await client.cancel_task(pb.CancelTaskRequest(id=held))
            seen = await asyncio.wait_for(follower, 45)
            rep.record("S42", bool(seen) and seen[0][0] == "task" and seen[-1][1] == pb.TASK_STATE_CANCELED,
                       f"SubscribeToTask on a live task -> {[(k, pb.TaskState.Name(s) if s else None) for k, s in seen][:4]}")
        except Exception as exc:
            rep.record("S42", False, f"resubscribe: {type(exc).__name__}: {str(exc)[:160]}")

    # BOLA: another principal cannot read the caller's task
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {caller_token}"}, timeout=60)
    other = httpx.AsyncClient(headers={"Authorization": f"Bearer {other_token}"}, timeout=60)
    async with http, other:
        try:
            mine = await (await ClientFactory(ClientConfig(httpx_client=http, streaming=False, supported_protocol_bindings=["JSONRPC"]))
                          .create_from_url(api, relative_card_path=card_path)).list_tasks(pb.ListTasksRequest(page_size=1))
            victim = mine.tasks[0].id
            theirs = await ClientFactory(ClientConfig(httpx_client=other, streaming=False, supported_protocol_bindings=["JSONRPC"])).create_from_url(api, relative_card_path=card_path)
            try:
                await theirs.get_task(pb.GetTaskRequest(id=victim))
                rep.record("S50", False, "another agent could read the caller's task")
            except Exception as exc:
                rep.record("S50", "TaskNotFound" in type(exc).__name__, f"another agent's GetTask on the caller's task -> {type(exc).__name__}")
            listed = await theirs.list_tasks(pb.ListTasksRequest(page_size=100))
            rep.record("S51", victim not in [t.id for t in listed.tasks], f"another agent's ListTasks excludes it ({len(listed.tasks)} own tasks)")
        except Exception as exc:
            rep.record("S50", False, f"BOLA probe: {type(exc).__name__}: {str(exc)[:160]}")


def raw_checks(rep: vs.Report, api: str, caller_token: str, callee_id: str) -> None:
    st, body = vs.http("GET", f"{api}/.well-known/agent-card.json")
    card = _json(body) or {}
    ifaces = card.get("supportedInterfaces") or []
    caps = card.get("capabilities") or {}
    rep.record("S01", st == 200 and len(ifaces) == 2 and all(i.get("url", "").startswith(api) for i in ifaces)
               and caps.get("streaming") is True and not caps.get("pushNotifications"),
               f"network card HTTP {st}: {[(i.get('protocolBinding'), i.get('protocolVersion')) for i in ifaces]}")
    st, body = vs.http("GET", f"{api}/v1/agents/{callee_id}/a2a-card")
    text = body.lower()
    acard = _json(body) or {}
    tenants = {i.get("tenant") for i in acard.get("supportedInterfaces") or []}
    rep.record("S02", st == 200 and tenants == {callee_id} and "a2a-proof.invalid" not in text and "public_key" not in text,
               f"agent card HTTP {st}: tenant = agent id, endpoint/key not exposed")
    st, body = vs.http("GET", f"{api}/v1/a2a/conformance")
    conf = _json(body) or {}
    rep.record("S03", st == 200 and bool(conf), f"conformance HTTP {st}: {sorted(conf)[:6]}")
    req = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": "x"}]}}}
    st, body = vs.http("POST", f"{api}/a2a", body=req, headers={"A2A-Version": "1.0"})
    rep.record("S04", st == 401, f"unauthenticated JSON-RPC -> HTTP {st}")
    st, body = vs.http("POST", f"{api}/a2a", body=req, token=caller_token)
    err = ((_json(body) or {}).get("error") or {}).get("code")
    rep.record("S05", err == -32009, f"missing A2A-Version (= 0.3) -> JSON-RPC error {err}")
    st, body = vs.http("POST", f"{api}/a2a", body=req, token=caller_token, headers={"A2A-Version": "1.0", "Content-Type": "text/plain"})
    rep.record("S06", st == 415, f"non-JSON content type -> HTTP {st}")
    st, body = vs.http("POST", f"{api}/a2a", body={"jsonrpc": "2.0", "id": 2, "method": "CreateTaskPushNotificationConfig", "params": {}},
                       token=caller_token, headers={"A2A-Version": "1.0"})
    err = ((_json(body) or {}).get("error") or {}).get("code")
    rep.record("S07", err in (-32003, -32602), f"push notification config -> JSON-RPC error {err} (not supported, declared false)")


def federation_proof(rep: vs.Report, api: str, op_token: str, ref_card: str) -> None:
    base = f"{api}/v1/a2a/federation"
    for code, url in (
        ("F01", "http://169.254.169.254/.well-known/agent-card.json"),
        ("F02", "https://registry.railway.internal/.well-known/agent-card.json"),
        ("F03", "https://127.0.0.1.nip.io/.well-known/agent-card.json"),
        ("F04", "https://user:pw@example.com/.well-known/agent-card.json"),
    ):
        st, body = vs.http("POST", f"{base}/agents", token=op_token, body={"cardUrl": url})
        rep.record(code, st == 422 and "refused" in body, f"discover {url.split('/')[2].split('@')[-1]} -> HTTP {st} {(_json(body) or {}).get('detail', '')[:60]}")
    if not ref_card:
        rep.record("F10", False, "A2A_REFERENCE_CARD_URL is not set")
        return
    st, body = vs.http("POST", f"{base}/agents", token=op_token, body={"cardUrl": ref_card})
    agent = _json(body) or {}
    rep.record("F10", st in (200, 201) and agent.get("state") in ("discovered", "verified") and agent.get("untrusted") is True,
               f"discover the reference agent -> HTTP {st} state={agent.get('state')} skills={[s.get('id') for s in agent.get('skills') or []][:3]}")
    rid = agent.get("id")
    if not rid:
        return
    st, body = vs.http("POST", f"{base}/agents/{rid}/state", token=op_token, body={"state": "verified", "reason": "Phase 8 live proof: official reference agent"})
    rep.record("F11", st == 200 and (_json(body) or {}).get("state") == "verified", f"operator verifies it -> HTTP {st}")
    st, body = vs.http("POST", f"{base}/connections", token=op_token, body={"remoteAgentId": rid, "label": "a2a-proof-reference", "authScheme": "none", "dailyCallLimit": 20})
    conn = _json(body) or {}
    rep.record("F12", st == 201 and conn.get("hasCredential") is False, f"connection (no credential) -> HTTP {st}")
    credential = "cred-" + uuid.uuid4().hex
    st, body = vs.http("POST", f"{base}/connections", token=op_token,
                       body={"remoteAgentId": rid, "label": "a2a-proof-sealed", "authScheme": "bearer", "credential": credential})
    sealed = _json(body) or {}
    _, listing = vs.http("GET", f"{base}/connections", token=op_token)
    leaked = credential in body + listing or "gAAAA" in body + listing
    rep.record("F13", st == 201 and sealed.get("hasCredential") is True and not leaked, f"bearer connection sealed, write-only (HTTP {st}, leaked={leaked})")
    if sealed.get("id"):
        st, _ = vs.http("DELETE", f"{base}/connections/{sealed['id']}", token=op_token)
        rep.record("F14", st in (200, 204), f"revoke destroys the sealed credential -> HTTP {st}")
    skill = ((agent.get("skills") or [{}])[0]).get("id") or "hello_world"
    st, body = vs.http("POST", f"{base}/calls", token=op_token, body={"connectionId": conn.get("id"), "skillId": skill, "input": {"text": "hello from AgentNet"}, "idempotencyKey": uuid.uuid4().hex})
    call = _json(body) or {}
    rep.record("F15", st == 201 and call.get("status") in ("sent", "succeeded") and call.get("untrusted") is True,
               f"outbound call through the official SDK client -> HTTP {st} status={call.get('status')} remoteState={call.get('remoteState')}")
    if call.get("id"):
        final = call
        for _ in range(10):
            if final.get("status") in ("succeeded", "failed"):
                break
            time.sleep(2)
            st, body = vs.http("POST", f"{base}/calls/{call['id']}/check", token=op_token)
            final = _json(body) or final
        summary = json.dumps(final.get("result") or {})
        rep.record("F16", final.get("status") == "succeeded" and final.get("remoteState") == "TASK_STATE_COMPLETED" and "hello" in summary.lower(),
                   f"GetTask on the remote task -> {final.get('status')} / {final.get('remoteState')} (result labelled untrusted)")
    st, body = vs.http("GET", f"{api}/v1/a2a/federation/summary")
    rep.record("F17", st == 200 and "verified" in body and "cardUrl" not in body, f"public federation summary is counts only -> HTTP {st}")


#: Role runs a live-model cycle must not be attributed to (NO FAKE AUTONOMY).
_NON_LIVE_MODELS = ("scripted", "fake", "stub", "mock", "offline")


def _cycle_correlation(api: str, op_token: str, cycle_id: str) -> Optional[str]:
    """The correlation id of the cycle's ``company.cycle`` event (operator API)."""
    st, body = vs.http("GET", f"{api}/v1/society/events?event_type=company.cycle&limit=100", token=op_token)
    for e in _json(body) or []:
        if e.get("subject_id") == cycle_id or (e.get("payload") or {}).get("cycle_id") == cycle_id:
            return e.get("correlation_id")
    return None


def company_proof(rep: vs.Report, api: str, op_token: str, timeout_s: int, existing: str = "") -> None:
    """Company cadence on the live Society. A cycle settles only once it is
    ``company.SETTLE_AFTER`` (30 min) old and every run of its correlation is
    terminal, so the timeout must exceed that. ``existing`` (a full id or an
    id prefix) follows a cycle that is already running instead of opening a
    new one, so a re-run never adds cycles."""
    st, body = vs.http("GET", f"{api}/v1/society/company", token=op_token)
    status = _json(body) or {}
    mode = status.get("mode") or {}
    rep.record("C01", st == 200 and mode.get("production_deploy_enabled") is False,
               f"operator company status -> HTTP {st} mode={json.dumps(mode, sort_keys=True)[:260]}")
    cycles = status.get("cycles") or []
    today = datetime.now(timezone.utc).date().isoformat()
    scheduled = [c for c in cycles if c.get("trigger") == "scheduled" and c.get("date") == today]
    rep.record("C05", bool(scheduled) or not mode.get("company_cycle_enabled"),
               f"scheduled cadence: {len(scheduled)} scheduled cycle(s) for {today} (at most one per UTC date)")
    if existing:
        match = [c for c in cycles if str(c.get("id", "")).startswith(existing)]
        cid = match[0]["id"] if len(match) == 1 else None
        rep.record("C02", bool(cid), f"follow existing company cycle {existing[:8]} -> {'found' if cid else 'not found (or ambiguous)'}")
    else:
        st, body = vs.http("POST", f"{api}/v1/society/company/cycles", token=op_token, body={})
        cid = (_json(body) or {}).get("id")
        rep.record("C02", st in (200, 201) and bool(cid), f"immediate company cycle -> HTTP {st} id={str(cid)[:8]}")
    if not cid:
        return
    deadline = time.time() + timeout_s
    outcome = None
    while time.time() < deadline:
        st, body = vs.http("GET", f"{api}/v1/society/company", token=op_token)
        for c in (_json(body) or {}).get("cycles") or []:
            if c.get("id") == cid and c.get("outcome"):
                outcome = c
        if outcome:
            break
        time.sleep(15)
    detail = {k: outcome.get(k) for k in ("outcome", "outcomeDetail", "createdAt")} if outcome else None
    rep.record("C03", bool(outcome), f"the Society settled the cycle: {json.dumps(detail, sort_keys=True)[:300] if outcome else 'not within the timeout'}")
    corr = _cycle_correlation(api, op_token, cid)
    if not corr:
        rep.record("C04", False, "the cycle's company.cycle event is not visible to the operator")
        return
    st, body = vs.http("GET", f"{api}/v1/society/runs?correlation_id={corr}&limit=200", token=op_token)
    runs = _json(body) or []
    models = sorted({f"{r.get('model_provider')}/{r.get('model_name')}" for r in runs})
    # A run that never reached cognition (e.g. suppressed) has no provider;
    # every run that DID think must have used the live provider.
    thought = [r for r in runs if r.get("model_provider")]
    live = bool(thought) and all(
        r.get("model_provider") == "openai_compatible"
        and not any(m in f"{r.get('model_provider')}/{r.get('model_name')}".lower() for m in _NON_LIVE_MODELS)
        for r in thought
    )
    roles = sorted({f"{r.get('agent_name')}:{r.get('event_type')}:{r.get('status')}" for r in runs})
    rep.record("C04", live, f"cycle correlation {corr[:8]}: {len(runs)} role run(s) on live model(s) {models}: {roles[:12]}")
    st, body = vs.http("GET", f"{api}/v1/society/story/{corr}", token=op_token)
    story = _json(body) or {}
    intents = sorted(
        f"{i.get('intent_type')}:{i.get('policy_decision')}:{i.get('execution_status')}"
        for r in story.get("runs") or [] for i in r.get("intents") or []
    )
    rep.record("C06", st == 200, f"cycle story -> HTTP {st}: {len(story.get('events') or [])} event(s), intents {intents[:20]}")


def incident_proof(rep: vs.Report, api: str, op_token: str) -> None:
    st, body = vs.http("POST", f"{api}/v1/society/incidents", token=op_token, body={"reason": "Phase 8 live proof: freeze drill", "source": "operator"})
    inc = _json(body) or {}
    rep.record("I01", st in (200, 201) and bool(inc.get("id")), f"open incident freeze -> HTTP {st}")
    if not inc.get("id"):
        return
    st, body = vs.http("GET", f"{api}/v1/society/company", token=op_token)
    rep.record("I02", inc["id"] in body, "company status lists the open incident")
    st, body = vs.http("POST", f"{api}/v1/society/incidents/{inc['id']}/lift", token=op_token, body={"reason": "drill complete"})
    rep.record("I03", st == 200, f"operator lifts it -> HTTP {st}")


def main() -> int:
    rep = vs.Report()
    api = env("REGISTRY_PUBLIC_URL").rstrip("/")
    secret = env("VALIDATOR_SECRET") or env("STAGING_VALIDATOR_SECRET")
    modes = {m.strip() for m in env("A2A_PROOF_MODES", "server").split(",") if m.strip()}
    user_email = env("VALIDATOR_USER_EMAIL", "staging-user@staging.agentnet.io.vn")
    other_email = env("A2A_PROOF_OTHER_EMAIL", "a2a-proof-other@staging.agentnet.io.vn")
    op_email = env("VALIDATOR_OPERATOR_EMAIL", "staging-operator@staging.agentnet.io.vn")
    sys.stdout.write(f"A2A PROOF start commit={env('RAILWAY_GIT_COMMIT_SHA', '?')[:12]} api={api} modes={sorted(modes)}\n")
    if not api or len(secret) < 32:
        rep.record("P00", False, "REGISTRY_PUBLIC_URL and a validator secret (>= 32 chars) are required")
        return finish(rep)
    password = vs.derive_password(secret)
    if modes & {"server"}:
        user_token = vs.ensure_user(rep, "P01", api, user_email, password)
        other_token_user = vs.ensure_user(rep, "P02", api, other_email, password)
        if not (user_token and other_token_user):
            return finish(rep)
        callee_id = ensure_agent(rep, "P03", api, user_token, secret, CALLEE_NAME)
        caller_id = ensure_agent(rep, "P04", api, user_token, secret, CALLER_NAME)
        other_id = ensure_agent(rep, "P05", api, other_token_user, secret, "A2A_Proof_Other")
        if not (callee_id and caller_id and other_id):
            return finish(rep)
        callee_token = agent_login(api, secret, CALLEE_NAME, callee_id)
        caller_token = agent_login(api, secret, CALLER_NAME, caller_id)
        other_token = agent_login(api, secret, "A2A_Proof_Other", other_id)
        rep.record("P06", bool(callee_token and caller_token and other_token), "agent logins (Ed25519) -> agent JWTs")
        if not (callee_token and caller_token and other_token):
            return finish(rep)
        raw_checks(rep, api, caller_token, callee_id)
        callee = Callee(api, callee_token)
        callee.start()
        try:
            asyncio.run(server_proof(rep, api, caller_token, other_token, callee_id))
        finally:
            callee.stop.set()
        rep.record("S60", len(callee.completed) >= 3, f"callee fulfilled {len(callee.completed)} task(s) over the ordinary REST API")
    if modes & {"federation", "company", "incident"}:
        op_token = vs.ensure_user(rep, "P10", api, op_email, password)
        if not op_token:
            return finish(rep)
        if "federation" in modes:
            federation_proof(rep, api, op_token, env("A2A_REFERENCE_CARD_URL"))
        if "incident" in modes:
            incident_proof(rep, api, op_token)
        if "company" in modes:
            company_proof(rep, api, op_token, int(env("A2A_PROOF_CYCLE_TIMEOUT", "2700")), env("A2A_PROOF_CYCLE_ID"))
    return finish(rep)


def finish(rep: vs.Report) -> int:
    verdict = "GREEN" if not rep.failed else "RED " + ",".join(rep.failed)
    sys.stdout.write(f"A2A PROOF RESULT: {verdict} ({rep.count} checks)\n")
    sys.stdout.flush()
    return 0 if not rep.failed else 1


if __name__ == "__main__":
    sys.exit(main())
