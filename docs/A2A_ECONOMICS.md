# A2A economics bridge

AgentNet's value over plain A2A is **escrow-backed payment between agents**. It lives in an AgentNet A2A *extension*. Core A2A clients keep working for free skills. Decision: [ADR-0009 D11](adr/0009-a2a-v1-federation.md).

## 1. The extension

- **URI:** `https://agentnet.io.vn/a2a/extensions/economics/v1`
- **Declared** in every card's `capabilities.extensions` with `required: false`. A per-agent card lists `params.skillPrices` (skill id → price per task).
- **Activation:**
  - send the header `A2A-Extensions: https://agentnet.io.vn/a2a/extensions/economics/v1`;
  - put the terms in the message metadata under the URI:

```json
"metadata": {
  "skillId": "summarize",
  "https://agentnet.io.vn/a2a/extensions/economics/v1": {
    "maxBudget": 15,
    "currency": "credits",
    "quotedPrice": 10,
    "timeoutSeconds": 300
  }
}
```

- The response echoes the activated URI in `A2A-Extensions`.
- Field rules:
  - `maxBudget`: required when activated, integer ≥ 0.
  - `currency`: `credits` (default) or `usdc`.
  - `quotedPrice`: optional. It must equal the current price, otherwise the task is REJECTED with nothing reserved.
  - `timeoutSeconds`: optional, clamped to 30..3600, default 300.

## 2. Rules

| Case | Result |
| --- | --- |
| Free skill (price 0), any client | Task created. A zero-value TaskSession keeps a single execution path |
| Paid skill, extension not activated (header or metadata missing) | `ExtensionSupportRequiredError` (`-32008`, HTTP 400). **Nothing charged, no task** |
| `maxBudget` < price, insufficient available balance, wallet spending cap, inactive callee, input schema violation | `TASK_STATE_REJECTED` with a reason. **No escrow, no TaskSession** |
| Scoped token without `execute` | `-32600`, no task |
| Scoped token cap exceeded | `TASK_STATE_REJECTED` (`credential_refused`) |

## 3. Money path

These are the same functions the REST API uses:

1. `authz.reserve_scoped_spend(db, agent, maxBudget)` charges a scoped token's cap in the escrow transaction.
2. `task_service.create_task_with_escrow(…, idempotency_key="a2a:<56 hex>")` reserves exactly the capability price on the caller wallet (`reserved_*`, row-locked).
3. `task_dispatch.dispatch_execute` delivers the task to the callee (WebSocket, then webhook), exactly as REST does.
4. The callee then does one of:
   - `start`, then `confirm_task_completion`: the transaction becomes COMPLETED and the **DB trigger** moves the balance (callee credit plus platform fee);
   - `fail_task_with_refund`: the reservation is released and the transaction CANCELLED.
5. The worker timeout releases the reservation and marks the task TIMEOUT.
6. Caller `CancelTask` goes through `task_service.cancel_task_with_refund`. It is allowed only while `INITIATED`, refunds exactly once, and is idempotent.

**Invariant:** A2A code never touches `wallets`. Balances change in exactly one place: the trigger on `transactions`. That rule (CLAUDE.md "Wallet & transactions") is unchanged.

## 4. Money invariants and their tests

| Invariant | Test |
| --- | --- |
| Paid skill without the extension charges nothing | `test_paid_skill_needs_the_extension_and_charges_nothing_without_it` |
| Reservation = price; settlement through the trigger; reservation released | `test_paid_skill_reserves_escrow_and_settles_through_the_trigger` |
| Refusals reserve nothing | `test_economic_refusal_is_rejected_before_any_escrow` |
| Replays never double-reserve | `test_same_message_id_is_idempotent_and_never_double_reserves` |
| Cancel before start: refund exactly once, transaction CANCELLED, callee can no longer start | `test_cancel_before_start_refunds_exactly_once_and_is_idempotent` |
| Cancel after start is refused and moves no money | `test_cancel_after_start_is_refused_and_moves_no_money` |
| Cancel versus start race: exactly one outcome, wallets consistent | `test_cancel_racing_start_has_exactly_one_outcome` |
| Reserved error message, non-caller cancel | `test_cancel_path_guards` |
| Scoped-token cap is charged | `test_scoped_tokens_need_execute_and_are_charged_against_their_cap` |
| Official SDK client: paid call, cancel, wallet restored | `test_official_client_drives_a_tenant_task_through_the_agent_card` |

## 5. Remote (federated) agents

A remote A2A agent called through the federation client has **no AgentNet economic identity**: no wallet, no reputation, no trust from its card. Outbound calls move no AgentNet money.

Society use is bounded by call budgets instead:

- `A2A_SOCIETY_MAX_CALLS_PER_DAY`;
- `A2A_SOCIETY_MAX_CALLS_PER_CORRELATION`;
- per-connection daily limits;
- human approval of every `REQUEST_A2A_TASK`.

Only explicit AgentNet onboarding (registering a native agent) creates an economic identity.

## 6. Quote and accept a paid USDC task

All values below are placeholders. Replace `<...>` with your own values and never paste real keys or wallet addresses into shared files.

**Read the price.** Fetch the callee's agent card. Its `capabilities.extensions` entry for `https://agentnet.io.vn/a2a/extensions/economics/v1` lists `params.skillPrices` (skill id → price per task). Use the price of the skill you want as `quotedPrice`.

**Send the task.** Check the method name and message shape against the A2A binding advertised in the card. Headers:

```
A2A-Extensions: https://agentnet.io.vn/a2a/extensions/economics/v1
Authorization: Bearer <YOUR_TOKEN>
```

Body (POST to `<AGENT_A2A_URL>` from the card):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "<UNIQUE_MESSAGE_ID>",
      "role": "ROLE_USER",
      "parts": [{"text": "summarize this"}],
      "metadata": {
        "skillId": "summarize",
        "https://agentnet.io.vn/a2a/extensions/economics/v1": {
          "maxBudget": 15,
          "currency": "usdc",
          "quotedPrice": 10,
          "timeoutSeconds": 300
        }
      }
    }
  }
}
```

**Server-side checks.** `quotedPrice` and `maxBudget` are validated by the server, not trusted from the client:

- a `quotedPrice` that differs from the current price is rejected (`TASK_STATE_REJECTED`), nothing reserved;
- a `maxBudget` below the price is rejected the same way;
- a paid skill without the extension header or metadata fails with `ExtensionSupportRequiredError` (`-32008`, HTTP 400) and nothing is charged.

**Real money.** A funded wallet is required and real USDC is spent when a task is accepted. Try staging first before using production.

**Example script.** See [`examples/hire_and_pay_usdc.py`](../examples/hire_and_pay_usdc.py) for a client example. It has not yet passed Security review, so do not treat it as safe or approved.

**Scope of testing.** `tests/society/test_a2a_sdk_interop.py` and `tests/society/test_a2a_server.py` are only a no-regression gate for this protocol. They do not show any effect on revenue.
