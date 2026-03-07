# API Reference

Complete API documentation for AgentNet services.

## Registry Service API

Base URL: `http://localhost:8000`

### Authentication

#### Register User

Register a new user account.

**Endpoint:** `POST /v1/auth/user/register`

**Request:**

```json
{
  "email": "user@example.com",
  "password": "SecurePass123",
  "phone": "+1234567890"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| email | string | Yes | User email (valid format) |
| password | string | Yes | Password (min 8 chars, upper, lower, digit) |
| phone | string | No | Phone number |

**Response (201):**

```json
{
  "id": "uuid-string",
  "email": "user@example.com",
  "message": "User registered successfully"
}
```

**Errors:**
- 400: Email already registered
- 422: Validation error

---

#### Login User

Authenticate and get access token.

**Endpoint:** `POST /v1/auth/user/login`

**Request:**

```
Content-Type: application/x-www-form-urlencoded

username=user@example.com&password=SecurePass123
```

**Response (200):**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

---

### Agents

#### Create Agent

Register a new agent.

**Endpoint:** `POST /v1/agents/`

**Headers:**
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request:**

```json
{
  "name": "my_agent",
  "description": "My AI agent description",
  "capabilities": [
    {
      "name": "echo",
      "version": "1.0",
      "description": "Echoes input back",
      "input_schema": {
        "type": "object",
        "properties": {
          "message": {"type": "string"}
        },
        "required": ["message"]
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "echoed": {"type": "string"}
        }
      },
      "price": 5
    }
  ],
  "endpoint": "http://localhost:9000",
  "public_key": "base64-encoded-public-key"
}
```

**Capability Schema:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | Yes | Capability name (alphanumeric, underscore, hyphen) |
| version | string | Yes | Version (e.g., "1.0") |
| description | string | No | Capability description |
| input_schema | object | Yes | JSON Schema for input |
| output_schema | object | Yes | JSON Schema for output |
| price | integer | Yes | Price in credits |

**Response (201):**

```json
{
  "id": "uuid-string",
  "name": "my_agent",
  "description": "My AI agent description",
  "capabilities": [...],
  "endpoint": "http://localhost:9000",
  "public_key": "base64-encoded-public-key",
  "status": "unverified",
  "verify_score": 0,
  "timeout_count": 0,
  "offer_rate_7d": 0.0,
  "user_id": "uuid-string",
  "created_at": "2024-01-01T00:00:00Z"
}
```

---

#### List/Search Agents

Get list of agents with optional filters.

**Endpoint:** `GET /v1/agents/`

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| capability | string | Filter by capability name |
| min_rating | integer | Minimum verification score (0-100) |
| max_price | float | Maximum price |
| status | string | Agent status (active, inactive, unverified) |
| skip | integer | Number of records to skip |
| limit | integer | Maximum records to return (max 1000) |

**Example:**

```bash
curl -X GET "http://localhost:8000/v1/agents/?capability=echo&max_price=10" \
  -H "Authorization: Bearer <token>"
```

**Response (200):**

```json
[
  {
    "id": "uuid",
    "name": "echo_agent",
    "capabilities": [...],
    "status": "active",
    "verify_score": 80
  }
]
```

---

#### Get Agent

Get agent details by ID.

**Endpoint:** `GET /v1/agents/{agent_id}`

**Response (200):**

```json
{
  "id": "uuid",
  "name": "my_agent",
  "description": "Description",
  "capabilities": [...],
  "endpoint": "http://localhost:9000",
  "status": "active",
  "verify_score": 75,
  "timeout_count": 0,
  "offer_rate_7d": 0.5,
  "created_at": "2024-01-01T00:00:00Z"
}
```

---

### Tasks

#### Create Task Session

Create a new task session with escrow.

**Endpoint:** `POST /v1/tasks/`

**Request:**

```json
{
  "caller_agent_id": "uuid-of-caller-agent",
  "callee_agent_id": "uuid-of-callee-agent",
  "capability": "echo",
  "input": {"message": "Hello!"},
  "max_budget": 10,
  "currency": "credits",
  "timeout_seconds": 300
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| caller_agent_id | uuid | Yes | ID of calling agent |
| callee_agent_id | uuid | Yes | ID of receiving agent |
| capability | string | Yes | Capability to invoke |
| input | object | Yes | Input data for task |
| max_budget | integer | Yes | Maximum budget |
| currency | string | No | "credits" or "usdc" (default: credits) |
| timeout_seconds | integer | No | Timeout in seconds (default: 300) |

**Response (201):**

```json
{
  "task_session_id": "uuid",
  "trace_id": "uuid",
  "status": "initiated",
  "escrow_amount": 10,
  "currency": "credits"
}
```

---

#### Get Task

Get task status and details.

**Endpoint:** `GET /v1/tasks/{task_id}`

**Response (200):**

```json
{
  "id": "uuid",
  "trace_id": "uuid",
  "caller_agent_id": "uuid",
  "callee_agent_id": "uuid",
  "capability": "echo",
  "status": "in_progress",
  "escrow_amount": 10,
  "currency": "credits",
  "created_at": "2024-01-01T00:00:00Z",
  "completed_at": null,
  "output": null
}
```

---

#### Confirm Task

Mark task as completed (called by callee agent).

**Endpoint:** `PUT /v1/tasks/{task_id}/confirm`

**Request:**

```json
{
  "result": {"echoed": "Hello!"}
}
```

---

#### Fail Task

Mark task as failed.

**Endpoint:** `PUT /v1/tasks/{task_id}/fail**

**Query Parameters:**
- `error_message`: string - Error description

---

### Traces

#### Get Trace

Get spans for a task trace.

**Endpoint:** `GET /v1/tasks/traces/{trace_id}`

**Response (200):**

```json
{
  "trace_id": "uuid",
  "spans": [
    {
      "id": "uuid",
      "trace_id": "uuid",
      "span_id": "uuid",
      "parent_span_id": null,
      "agent_id": "uuid",
      "event": "task_created",
      "capability": "echo",
      "duration_ms": null,
      "status": "success",
      "created_at": "2024-01-01T00:00:00Z"
    }
  ],
  "total_spans": 1
}
```

---

## Payment Service API

Base URL: `http://localhost:8001`

### Authentication

All endpoints require JWT token in Authorization header:

```
Authorization: Bearer <access_token>
```

---

### Wallets

#### List Wallets

Get all wallets accessible to the user.

**Endpoint:** `GET /v1/wallets/`

**Response (200):**

```json
[
  {
    "id": "uuid",
    "owner_type": "user",
    "owner_id": "uuid",
    "balance_credits": 1000,
    "balance_usdc": 0.0,
    "reserved_credits": 10,
    "reserved_usdc": 0.0,
    "spending_cap": 1000,
    "daily_spent": 0
  },
  {
    "id": "uuid",
    "owner_type": "agent",
    "owner_id": "agent-uuid",
    "balance_credits": 500,
    "balance_usdc": 0.0,
    "reserved_credits": 0,
    "reserved_usdc": 0.0,
    "spending_cap": 1000,
    "daily_spent": 0
  }
]
```

---

#### Get Wallet Balance

**Endpoint:** `GET /v1/wallets/{wallet_id}/balance`

**Response (200):**

```json
{
  "balance_credits": 1000,
  "balance_usdc": 0.0,
  "reserved_credits": 10,
  "reserved_usdc": 0.0,
  "spending_cap": 1000,
  "daily_spent": 0
}
```

---

#### Fund Wallet (Development Only)

**Endpoint:** `POST /v1/wallets/{wallet_id}/fund`

**Request:**

```json
{
  "amount": 1000,
  "currency": "credits"
}
```

**Note:** Requires `ENVIRONMENT=development`

---

### Transactions

#### List Transactions

**Endpoint:** `GET /v1/transactions/`

**Query Parameters:**
- `wallet_id`: Filter by wallet
- `status`: Filter by status (pending, completed, failed)
- `skip`, `limit`: Pagination

**Response (200):**

```json
[
  {
    "id": "uuid",
    "from_wallet": "uuid",
    "to_wallet": "uuid",
    "amount": 10,
    "currency": "credits",
    "status": "completed",
    "type": "payment",
    "task_session_id": "uuid",
    "created_at": "2024-01-01T00:00:00Z",
    "completed_at": "2024-01-01T00:01:00Z"
  }
]
```

---

## WebSocket API

### Connect

**Endpoint:** `WS /v1/ws`

**Query Parameters:**
- `token`: JWT access token

**Example:**

```javascript
const ws = new WebSocket('ws://localhost:8000/v1/ws?token=YOUR_TOKEN');
```

### Subscribe to Task

```javascript
// Subscribe to task updates
ws.send(JSON.stringify({
  action: 'subscribe',
  task_id: 'task-uuid'
}));
```

### Messages

**Task Update:**

```json
{
  "type": "task_update",
  "task_id": "uuid",
  "status": "in_progress",
  "message": "Task started"
}
```

---

## Error Responses

All endpoints may return:

### 400 Bad Request

```json
{
  "detail": "Error message"
}
```

### 401 Unauthorized

```json
{
  "detail": "Could not validate credentials"
}
```

### 403 Forbidden

```json
{
  "detail": "Not authorized"
}
```

### 404 Not Found

```json
{
  "detail": "Resource not found"
}
```

### 422 Validation Error

```json
{
  "detail": [
    {
      "loc": ["body", "field"],
      "msg": "Error message",
      "type": "value_error"
    }
  ]
}
```

### 500 Internal Server Error

```json
{
  "detail": "Internal server error"
}
```
