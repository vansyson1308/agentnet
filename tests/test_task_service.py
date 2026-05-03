"""
Regression tests for the unified task_service module.

These exercise the invariants (no live DB needed):
1. The module exposes the four lifecycle functions REST + WS share.
2. The WebSocket route passes args to handle_message in the right order.
3. Routes/tasks.py no longer has its own escrow logic — it delegates to
   task_service. Catches refactor regressions where someone re-adds
   wallet mutation in the route.
4. Worker.process_timed_out_tasks does its work in a single atomic
   commit (no split status/wallet commits).
5. Inline span persistence — task creation no longer offloads the initial
   span to background_tasks.add_task.
"""

import inspect
import pathlib
import re

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (REPO / rel_path).read_text()


class TestTaskServiceShape:
    def test_module_exports_lifecycle_functions(self):
        from services.registry.app import task_service

        for name in (
            "create_task_with_escrow",
            "start_task",
            "confirm_task_completion",
            "fail_task_with_refund",
            "EscrowError",
        ):
            assert hasattr(task_service, name), f"task_service missing {name!r}"

    def test_create_task_signature_uses_keyword_only(self):
        from services.registry.app import task_service

        sig = inspect.signature(task_service.create_task_with_escrow)
        # All parameters keyword-only — protects against positional-arg
        # bugs like the one we just fixed in websocket route.
        for name, param in sig.parameters.items():
            assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"create_task_with_escrow.{name} must be keyword-only"
            )

    def test_create_task_takes_idempotency_key(self):
        from services.registry.app import task_service

        sig = inspect.signature(task_service.create_task_with_escrow)
        assert "idempotency_key" in sig.parameters


class TestWebsocketRouteArgOrder:
    """The bug we fixed: route called handle_message(message, agent_id, db).
    The signature is handle_message(message, db, connection_id). Any
    regression here will silently break ALL WS messaging.
    """

    def test_route_passes_db_then_connection_id(self):
        text = _read("services/registry/app/api/routes/websocket.py")
        # The fixed call should pass connection_id (not agent_id) as third arg.
        assert "manager.handle_message(message, db, connection_id)" in text, (
            "websocket route must call handle_message with (message, db, "
            "connection_id) — see services/registry/app/websocket_manager.py:189"
        )
        # Make sure the broken call is gone.
        assert "manager.handle_message(message, agent_id, db)" not in text

    def test_handle_message_signature_matches(self):
        # Import-light: don't pull in the full ConnectionManager (which
        # transitively requires the optional `ed25519` build artifact);
        # parse the source instead.
        text = _read("services/registry/app/websocket_manager.py")
        m = re.search(
            r"async def handle_message\(self,\s*([^)]+)\):",
            text,
        )
        assert m is not None, "handle_message definition missing"
        params = [p.strip().split(":")[0].strip() for p in m.group(1).split(",")]
        assert params == ["message", "db", "connection_id"], params


class TestWebsocketTaskHandlersExist:
    """The "would be preserved here" comments meant the WS task path was
    actually missing. Make sure the dispatch table now covers execute and
    callee transitions."""

    def test_handle_message_dispatches_task_methods(self):
        text = _read("services/registry/app/websocket_manager.py")
        # Expect dispatcher to enumerate the methods.
        for method in ("execute", "task_start", "task_confirm", "task_fail"):
            assert method in text, f"WS dispatcher missing {method!r} method"
        # Expect concrete handler that calls task_service.
        assert "_handle_task_method" in text
        assert "create_task_with_escrow" in text
        assert "fail_task_with_refund" in text


class TestRoutesUseTaskService:
    """Routes must NOT re-implement escrow logic; they only handle auth + framing."""

    def test_routes_tasks_imports_task_service(self):
        text = _read("services/registry/app/api/routes/tasks.py")
        assert "from ...task_service import" in text
        for name in (
            "create_task_with_escrow",
            "confirm_task_completion",
            "fail_task_with_refund",
        ):
            assert name in text

    def test_routes_no_longer_mutate_reserved_directly(self):
        text = _read("services/registry/app/api/routes/tasks.py")
        # Pre-refactor lines like
        #   caller_wallet.reserved_credits += price
        #   caller_wallet.reserved_credits = max(0, ...)
        # leak the invariant out of task_service. Catch them.
        forbidden_patterns = [
            r"caller_wallet\.reserved_credits\s*\+=",
            r"caller_wallet\.reserved_credits\s*=\s*max",
            r"caller_wallet\.reserved_usdc\s*\+=",
            r"caller_wallet\.reserved_usdc\s*=\s*max",
        ]
        for pat in forbidden_patterns:
            assert not re.search(pat, text), (
                f"routes/tasks.py contains forbidden direct reserved mutation: {pat!r}"
            )


class TestWorkerAtomicRefund:
    def test_worker_refund_uses_for_update(self):
        text = _read("services/worker/app/worker.py")
        # The atomic version uses .with_for_update() on the task, the
        # transaction, and the caller wallet so concurrent confirm can't
        # double-decrement reserved.
        assert text.count(".with_for_update()") >= 3, (
            "worker.process_timed_out_tasks must take FOR UPDATE locks on "
            "task, transaction, and wallet rows"
        )

    def test_worker_no_split_commit(self):
        text = _read("services/worker/app/worker.py")
        # The original code had two db.commit()s per timed-out task. After
        # the refactor it's exactly one (status + wallet in same txn).
        # Heuristic: count commit() calls inside process_timed_out_tasks.
        m = re.search(
            r"async def process_timed_out_tasks\([^)]*\):.*?(?=\n(?:async )?def )",
            text,
            re.DOTALL,
        )
        body = m.group(0) if m else text
        commit_calls = body.count("db.commit()")
        assert commit_calls == 1, (
            f"process_timed_out_tasks should commit exactly once per task; "
            f"found {commit_calls} db.commit() calls"
        )


class TestInlineSpanPersistence:
    """Spans are now part of the same DB transaction as the task transition,
    so a failed span insert rolls the task back too."""

    def test_create_task_does_not_offload_initial_span(self):
        text = _read("services/registry/app/task_service.py")
        # task_service should add the Span object and commit alongside
        # task_session, not background_tasks.add_task(save_span).
        assert "background_tasks.add_task" not in text
        assert "db.add(\n            Span(" in text or "db.add(\n        Span(" in text or "Span(" in text


class TestIdempotencyColumn:
    def test_transaction_model_has_idempotency_key(self):
        from services.registry.app.models import Transaction

        assert "idempotency_key" in Transaction.__table__.columns, (
            "Transaction model must declare idempotency_key column"
        )
        col = Transaction.__table__.columns["idempotency_key"]
        assert col.unique is True or col.index is True, (
            "idempotency_key must have a unique/indexed constraint"
        )

    def test_payment_and_worker_models_match(self):
        from services.payment.app.models import Transaction as PaymentTx
        from services.worker.app.models import Transaction as WorkerTx

        assert "idempotency_key" in PaymentTx.__table__.columns
        assert "idempotency_key" in WorkerTx.__table__.columns

    def test_init_db_migration_present(self):
        path = REPO / "services/registry/init-db/13-idempotency.sql"
        assert path.exists(), "13-idempotency.sql migration missing"
        text = path.read_text()
        assert "idempotency_key" in text
        assert "UNIQUE" in text.upper()


class TestCapMigration:
    def test_spending_cap_migration_present(self):
        path = REPO / "services/registry/init-db/14-spending-cap-fix.sql"
        assert path.exists()
        text = path.read_text()
        assert "reserved" in text.lower()
        assert "check_spending_cap" in text
