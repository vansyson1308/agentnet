# Database Schema Contract

AgentNet is an escrow + wallet system: the database schema is a correctness
surface, not an implementation detail. This document states **which artifact
is authoritative for what**, the rules every ORM must follow, and how to add
schema. It is enforced by `tests/test_db_parity.py` (run in CI) and
`deploy/society-migration-check.sh`.

## 1. Artifacts and who is authoritative

| Artifact | Role | Authoritative for |
|---|---|---|
| `services/registry/init-db/*.sql` (the *bundle*, `01-init.sql` … `17-app-tables.sql`) | **Full bootstrap** of an empty database, applied in lexical order | Column types, nullability, defaults, constraints, indexes, triggers, enum types, seed rows (admin/platform users, platform wallet) |
| `services/registry/migrations/versions/*.py` (alembic) | **Incremental** changes for databases that already ran the bundle | The *delta* between bundle snapshots; never rewrites core tables |
| `services/registry/app/society/schema_sql.py`, `services/registry/app/schema_app_sql.py` | **Single-source DDL modules** | The society runtime tables (bundle file 16, migrations 0007/0008) and the application tables (bundle file 17, migration 0009). The bundle files are *generated* from these modules and must stay byte-identical |
| Service ORMs (`services/*/app/models.py`) | Typed access to the schema above | Nothing on the DB side. An ORM never creates schema in a deployment (`create_all` is not called by any service) — the DDL is the truth and the ORM mirrors it |

### How a database gets its schema

```
fresh Postgres volume ──► postgres image runs /docker-entrypoint-initdb.d = init-db/*.sql ──┐
                                                                                            ├─► registry entrypoint:
empty managed/CI DB  ──► registry entrypoint: python -m app.db_bootstrap --init-dir /app/init-db ─┘   alembic stamp 0003_spending_cap_fix
                                                                                                    alembic upgrade head  (0004.. idempotent)

already-migrated DB  ──► registry entrypoint: alembic upgrade head only
```

`services/registry/entrypoint.sh` decides with one probe:

| probe result | exit code | action |
|---|---|---|
| `alembic_version` exists | 0 | `alembic upgrade head` |
| schema present (`public.transactions`) but never stamped | 10 | `alembic stamp 0003_spending_cap_fix` → `upgrade head` |
| empty database | 20 | `python -m app.db_bootstrap --init-dir "$INIT_DB_DIR"` (whole bundle, one transaction per file, fails hard) → `stamp 0003` → `upgrade head` |

`INIT_DB_DIR` defaults to `/app/init-db` (the image copies `init-db/` there). A
local checkout or a fresh-install harness runs the same entrypoint with
`INIT_DB_DIR=<checkout>/services/registry/init-db POSTGRES_DB=<db> bash entrypoint.sh true`
from `services/registry` (needs `alembic` on PATH); `python -m app.db_bootstrap`
honours the same variable when `--init-dir` is omitted. The parity suite runs this
exact command twice (`test_entrypoint_bootstraps_empty_database_end_to_end`) and
asserts head after the first run and a no-op second run.

Why stamp **0003**: `0001_baseline` is a deliberate no-op, `0002`/`0003` mirror
bundle files `13-idempotency.sql`/`14-spending-cap-fix.sql`, and every later
migration (`0004`..`0009`) uses `IF NOT EXISTS` / `CREATE OR REPLACE` so it is a
no-op over a bundle that already contains the same objects.

**Both paths converge.** `tests/test_db_parity.py` builds one scratch database
from the whole bundle and another from bundle files `01..15` + `alembic stamp
0003` + `upgrade head`, snapshots both (tables, columns with type/udt/nullability/
default/length/precision, primary keys, unique/check/FK constraints, indexes,
triggers, enum labels) and asserts the snapshots are identical; it also proves
alembic is a schema no-op on top of the bundle and that `downgrade
0008_society_phase2 → upgrade head` round-trips. `deploy/society-migration-check.sh
--mode local|docker` proves the same on a host (fresh + upgrade paths, expected
head `0009_app_tables`).

## 2. ORM rules

### 2.1 Which ORM is the full definition of a table

* **registry** (`services/registry/app/models.py`) — full definition of every
  table it declares (core, social graph, goals/memory/improvements, society
  runtime, provisioning/app tables). A DB column missing from the registry ORM
  is a test failure.
* **simulation** (`services/simulation/app/models.py`) — full definition of the
  `sim_*` tables.
* **payment** (`services/payment/app/models.py`) — full definition of
  `approval_requests` only; a *subset* of users/agents/wallets/task_sessions/
  transactions.
* **worker** (`services/worker/app/models.py`) — subsets only.

Tables with **no ORM by design**: `daily_spending` (written exclusively by the
`check_spending_cap` / `update_daily_spending` triggers), `alembic_version`.
`agent_connection_strength` is a materialized view, not a table.

### 2.2 Service-specific subset rule

A payment/worker model may omit a column the service never touches **only if
that column is nullable or has a DB default** — otherwise an insert from that
service could never succeed (e.g. `users.password_hash` is `NOT NULL` without a
default, so `worker.User` must carry it). Every column a subset *does* declare
must have the same name, compatible type and identical nullability as the DB.
Both halves are enforced per table by `test_orm_table_matches_database`.

### 2.3 Enum representation

* The **Postgres enum types are the truth**: `kyc_status`, `agent_status`,
  `wallet_owner_type`, `task_status`, `span_status`, `transaction_status`,
  `transaction_type`, `referral_status`, `offer_status`, `currency_type`,
  `interaction_type` (01/05-init) and `sim_status` (06-simulation).
* registry / payment / worker bind **strings** through
  `_enum_column(...)` = `Enum(cls, native_enum=False, values_callable=...)`.
  No service may declare a native enum with an auto-derived name (`agentstatus`,
  `taskstatus`, …): those types do not exist and `create_all` would try to create
  them. The worker used to do exactly this; it now shares the helper.
* simulation references the pre-existing type with the PG-dialect
  `postgresql.ENUM(..., name="sim_status", create_type=False)`. Note that the
  generic `sqlalchemy.Enum` silently *drops* `create_type`, which is why the
  dialect type is used explicitly.
* Label parity: for every ORM Enum column bound to a Postgres enum, the
  `pg_enum` labels must equal the Python enum values; every DB enum type must
  be bound by at least one ORM column (`test_enum_labels_match_python_enums`).
* **TEXT + CHECK pseudo-enums** (`goals.owner_type/priority/status`,
  `improvement_proposals.source/status/target_scope`, `memory_items.scope`)
  are compared against the values in the CHECK constraint; a CHECK is required
  on those tables.
* Society runtime status columns (`society_events.status`, `agent_runs.status`,
  `agent_intents.*`, `intent_approvals.decision`, `code_candidates.status`,
  `agent_capability_grants.risk_ceiling`) and `approval_requests.status` are
  plain `VARCHAR(n)` **without** a CHECK (intentional: the runtime state machine
  is validated in code and these evolve often). The ORM still binds string enums.

### 2.4 Timestamps: naive vs `TIMESTAMPTZ` per table family

The ORM mirrors the DB column type exactly (`NaiveTimestamp = DateTime(timezone=False)`,
`TzTimestamp = DateTime(timezone=True)` in each models module). It does **not**
change the DB type; changing core table types would be a data migration and is out
of scope.

| `TIMESTAMP` (naive) — created by 01..07 | `TIMESTAMPTZ` — everything later |
|---|---|
| `users`, `agents` (incl. `reputation_updated_at`), `wallets`, `task_sessions`, `spans`, `transactions`, `referrals`, `offers`, `negotiation_rounds`, `agent_interactions`, `notifications`, `daily_spending`, `sim_*` | `agents.last_seen_at` (08-heartbeat), `email_verification_tokens`, `agent_reputation_history`, `stories`, `goals`, `improvement_proposals`, `memory_items`, `agent_chat`, all society runtime tables, all app tables (`audit_log`, `projects`, `scoped_tokens`, …, `approval_requests`) |

Practical consequence: values read from naive columns come back as naive
`datetime`s; compare them with `datetime.utcnow()`-style naive values (the
services already do). Writing an aware datetime into a naive column makes
Postgres convert to the session time zone and drop the offset.

### 2.5 Money columns

`wallets.balance_credits/reserved_credits/spending_cap/daily_spent/auto_approve_threshold`,
`task_sessions.escrow_amount`, `spans.credits_used`, `transactions.amount/platform_fee`,
`referrals.reward_amount`, `offers.price`, `negotiation_rounds.proposed_price`,
`agents.total_volume_credits`, `agent_interactions.total_volume` and
`approval_requests.amount` are `BIGINT` in the DB and `BigInteger` in every ORM.
Python semantics are unchanged (plain `int`). **Balances are mutated only by the
DB triggers** (`update_wallet_balances`, `update_daily_spending`); no ORM change
in this contract touches trigger or balance logic.

### 2.6 Other structural facts the ORM must mirror

* `agent_reputation_history` has the composite primary key
  `(agent_id, snapshot_date)` and **no surrogate id**; `reputation.record_reputation_snapshot`
  upserts with `ON CONFLICT (agent_id, snapshot_date)`.
* Composite uniques: `agent_interactions (from_agent_id, to_agent_id, interaction_type)`,
  `agent_runs uq_agent_runs_agent_event (agent_id, event_id)`,
  `agent_intents uq_agent_intents_run_seq (run_id, seq)`; the partial unique
  index on `transactions.idempotency_key`.
* FK `ON DELETE`: `notifications.user_id` and `email_verification_tokens.user_id`
  are nullable + `CASCADE`; `negotiation_rounds.offer_id` `CASCADE`.
* `agents.is_online / last_seen_at / current_capability` (08-heartbeat) are in
  the registry and worker ORMs (previously assigned by `websocket_manager` but
  absent from the ORM — silently never written).
* `spans.extra_data`: `01-init.sql` created the column as `metadata`; file 16 /
  migration 0007 rename it in place. Fresh and upgraded databases both end with
  `extra_data`.

### 2.7 What parity does *not* compare (by design)

* Python-side `default=` values vs DB `DEFAULT`s (only nullability and type).
* Non-unique index names (e.g. the ORM's `index=True` on `sim_sessions.user_id`
  vs the DDL's `idx_sim_sessions_user`) — indexes are an operational concern of
  the DDL; the bundle-vs-alembic comparison does check them.
* Trigger/function bodies (only trigger definitions, not PL/pgSQL text).

## 3. How to add or change schema

### Adding a table

1. Put the DDL in a single-source module — `app/schema_app_sql.py` for
   application tables, `app/society/schema_sql.py` for society runtime tables,
   or a new module for a new family. Use `CREATE TABLE IF NOT EXISTS` /
   `CREATE INDEX IF NOT EXISTS`, `TIMESTAMPTZ`, `BIGINT` for money, the shared
   enum types where applicable, and a `DEFAULT gen_random_uuid()` id.
2. Regenerate the bundle file so it is byte-identical to the module, e.g.
   `cd services/registry && python -c "from app.schema_app_sql import APP_TABLES_SQL; open('init-db/17-app-tables.sql','w').write(APP_TABLES_SQL)"`
   (a new family gets a new `NN-name.sql`).
3. Add an alembic migration whose `upgrade()` executes the module's SQL and
   whose `downgrade()` drops exactly its tables; chain it from the current head.
4. Add the ORM model(s) — mirroring types/nullability — to the owning service
   (and faithful subsets elsewhere if needed).
5. Run `pytest tests/test_db_parity.py tests/society/test_schema_and_migrations.py`
   and `bash deploy/society-migration-check.sh --mode local`; bump
   `EXPECTED_HEAD` there and the head assertions in the society schema test.

### Adding a column to an existing table

Write it **twice, identically and idempotently**: `ALTER TABLE … ADD COLUMN IF
NOT EXISTS …` in a new bundle file *or* in the family's SQL module (regenerate the
bundle file), **and** in a new alembic migration. Then mirror it in every ORM that
owns or subsets the table. The parity test fails if the two paths diverge.

### Never

* Change a core table's column type in a migration without a data-migration plan.
* Add `create_all` to a service start-up path.
* Declare a native enum in an ORM.
* Touch `wallets`/`transactions` triggers or balance logic without a regression
  test and a money-invariant note (CLAUDE.md §3).

## 4. Verification commands

```bash
# schema contract (skips only if Postgres is unreachable)
POSTGRES_HOST=127.0.0.1 POSTGRES_USER=agentnet POSTGRES_PASSWORD="" JAEGER_ENABLED=false \
  pytest tests/test_db_parity.py tests/society/test_schema_and_migrations.py -q

# fresh + upgrade paths on scratch databases, expected head 0009_app_tables
bash deploy/society-migration-check.sh --mode local

# bootstrap an empty database exactly like the entrypoint does
cd services/registry && POSTGRES_DB=some_empty_db python -m app.db_bootstrap --init-dir init-db \
  && alembic stamp 0003_spending_cap_fix && alembic upgrade head
```
