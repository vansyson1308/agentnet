// Interoperability proof: the OFFICIAL @a2a-js/sdk client (pinned 1.2.1)
// drives AgentNet over both bindings. Nothing on the client side is
// hand-rolled: card resolution, interface selection, the tenant decorator and
// the wire format all come from the SDK.
//
//   cd scripts/a2a && npm ci && \
//   AGENTNET_BASE_URL=https://api.agentnet.io.vn \
//   AGENTNET_A2A_TOKEN=<agent JWT or spt_ token, never printed> \
//   AGENTNET_TENANT=<marketplace agent id with a FREE skill> AGENTNET_SKILL=<skill id> \
//   node js_interop.mjs
//
// Prints one JSON summary (ids, states, counts -- never the credential).
import { Role, TaskState } from "@a2a-js/sdk";
import { ClientFactory, ClientFactoryOptions, JsonRpcTransportFactory, RestTransportFactory } from "@a2a-js/sdk/client";
import { randomUUID } from "node:crypto";

const base = (process.env.AGENTNET_BASE_URL || "").replace(/\/$/, "");
const token = process.env.AGENTNET_A2A_TOKEN || "";
const tenant = process.env.AGENTNET_TENANT || "";
const skill = process.env.AGENTNET_SKILL || "";
if (!base || !token) {
  console.error("AGENTNET_BASE_URL and AGENTNET_A2A_TOKEN are required");
  process.exit(2);
}

// Bearer on every A2A request; the public card fetch does not need it but it is harmless.
const authFetch = (input, init = {}) => {
  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
};

function factoryFor(binding) {
  return new ClientFactory(
    ClientFactoryOptions.createFrom(ClientFactoryOptions.default, {
      transports: [new JsonRpcTransportFactory({ fetchImpl: authFetch }), new RestTransportFactory({ fetchImpl: authFetch })],
      preferredTransports: [binding],
    }),
  );
}

function userMessage(parts, metadata) {
  return {
    messageId: randomUUID(),
    contextId: "",
    taskId: "",
    role: Role.ROLE_USER,
    parts,
    metadata,
    extensions: [],
    referenceTaskIds: [],
  };
}

const text = (value) => ({ content: { $case: "text", value }, metadata: undefined, filename: "", mediaType: "text/plain" });
const data = (value) => ({ content: { $case: "data", value }, metadata: undefined, filename: "", mediaType: "application/json" });

const summary = { sdk: "@a2a-js/sdk@1.2.1", base, bindings: {} };
for (const binding of ["JSONRPC", "HTTP+JSON"]) {
  const out = {};
  try {
    const network = await factoryFor(binding).createFromUrl(base);
    const search = await network.sendMessage({ tenant: "", message: userMessage([text("agent")], undefined), configuration: undefined, metadata: undefined });
    const msg = search.payload?.value ?? search.message ?? search;
    out.networkSearch = { kind: msg.messageId ? "message" : "task", role: msg.role, parts: (msg.parts || []).length };
    if (tenant) {
      const agent = await factoryFor(binding).createFromUrl(base, `/v1/agents/${tenant}/a2a-card`);
      const sent = await agent.sendMessage({
        tenant: "",
        message: userMessage([data({ source: "a2a-js-interop", binding })], skill ? { skillId: skill } : undefined),
        configuration: { acceptedOutputModes: [], taskPushNotificationConfig: undefined, historyLength: undefined, returnImmediately: true },
        metadata: undefined,
      });
      const task = sent.payload?.value ?? sent.task ?? sent;
      if (!task.status) throw new Error("expected a Task for a tenant skill");
      out.task = { id: task.id, state: TaskState[task.status?.state] ?? task.status?.state };
      const got = await agent.getTask({ tenant: "", id: task.id, historyLength: undefined });
      out.getTask = { state: TaskState[got.status?.state] ?? got.status?.state, history: (got.history || []).length };
      const listed = await agent.listTasks({ tenant: "", contextId: "", status: 0, pageSize: 5, pageToken: "", historyLength: undefined, statusTimestampAfter: undefined, includeArtifacts: undefined });
      out.listTasks = { returned: (listed.tasks || []).length, nextPageToken: listed.nextPageToken };
      const canceled = await agent.cancelTask({ tenant: "", id: task.id, metadata: undefined });
      out.cancelTask = { state: TaskState[canceled.status?.state] ?? canceled.status?.state };
    }
    out.ok = true;
  } catch (err) {
    out.ok = false;
    out.error = `${err?.name || "Error"}: ${String(err?.message || err).slice(0, 300)}`;
  }
  summary.bindings[binding] = out;
}
console.log(JSON.stringify(summary, null, 2));
process.exit(Object.values(summary.bindings).every((b) => b.ok) ? 0 : 1);
