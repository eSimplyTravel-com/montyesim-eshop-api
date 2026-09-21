import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import DatabaseException
from app.repo.stripe_event_repo import (DuplicateEventError, LEASE_SECONDS, StripeEventRepo)
from app.services.callback_service import CallbackService


class FakeQuery:
    """Chainable stand-in for the PostgREST builder; records the filters it was given."""

    def __init__(self, rows, calls):
        self._rows = rows
        self.calls = calls

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return self
        return call

    def execute(self):
        self.calls.append(("execute", ()))
        return MagicMock(data=self._rows)


@pytest.fixture
def repo():
    repo = object.__new__(StripeEventRepo)
    repo.table = MagicMock()
    repo.model = MagicMock()
    repo.calls = []
    return repo


@pytest.fixture
def service():
    service = object.__new__(CallbackService)
    service._CallbackService__stripe_event_repo = MagicMock()
    service._CallbackService__task_executor = MagicMock()
    return service


def _webhook_request(body=b"{}"):
    request = MagicMock()

    async def body_coro():
        return body

    request.body = body_coro
    request.headers = {"stripe-signature": "sig"}
    return request


def _event():
    return {"id": "evt_1", "type": "payment_intent.succeeded", "api_version": "2024-06-20"}


# --- recording: duplicate vs infrastructure failure -------------------------------------------

def test_record_raises_duplicate_on_primary_key_clash(repo):
    error = Exception('duplicate key value violates unique constraint "stripe_event_pkey"')
    repo.table.insert.return_value.execute.side_effect = error
    repo.table.select.return_value = FakeQuery([{"id": "evt_1"}], [])  # the row really is there
    with pytest.raises(DuplicateEventError):
        repo.record("evt_1", "payment_intent.succeeded", payload="{}")


def test_record_raises_database_error_when_supabase_is_down(repo):
    repo.table.insert.return_value.execute.side_effect = Exception("connection refused")
    with pytest.raises(DatabaseException):
        repo.record("evt_1", "payment_intent.succeeded", payload="{}")


def test_webhook_acknowledges_a_duplicate_without_queueing(service):
    service._CallbackService__stripe_event_repo.record.side_effect = DuplicateEventError("evt_1")
    with patch("app.services.callback_service.stripe.Webhook.construct_event", return_value=_event()):
        asyncio.run(service.handle_payment_webhook(_webhook_request()))
    service._CallbackService__task_executor.add_task.assert_not_called()


def test_webhook_refuses_to_acknowledge_when_the_event_was_not_stored(service):
    from fastapi import HTTPException
    service._CallbackService__stripe_event_repo.record.side_effect = DatabaseException("down")
    with patch("app.services.callback_service.stripe.Webhook.construct_event", return_value=_event()):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(service.handle_payment_webhook(_webhook_request()))
    assert raised.value.status_code == 500  # Stripe retries; the payment is not lost
    service._CallbackService__task_executor.add_task.assert_not_called()


def test_webhook_stores_the_payload_for_later_replay(service):
    with patch("app.services.callback_service.stripe.Webhook.construct_event", return_value=_event()):
        asyncio.run(service.handle_payment_webhook(_webhook_request()))
    stored = service._CallbackService__stripe_event_repo.record.call_args.kwargs["payload"]
    assert json.loads(stored)["id"] == "evt_1"


# --- claiming: ownership and leases ------------------------------------------------------------

def _claim_repo(repo, claimed):
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.return_value = MagicMock(data=claimed)
    return repo.client


def test_claim_uses_the_atomic_sql_function(repo):
    client = _claim_repo(repo, claimed=True)
    token = repo.claim("evt_1", max_attempts=5)
    assert token
    name, params = client.rpc.call_args[0]
    assert name == "claim_stripe_event"
    # eligibility, attempt increment and the lease are all decided inside one statement
    assert params["p_event_id"] == "evt_1" and params["p_max_attempts"] == 5
    assert params["p_token"] == token and params["p_lease_seconds"] == LEASE_SECONDS


def test_claim_returns_none_when_the_row_is_not_claimable(repo):
    _claim_repo(repo, claimed=False)
    assert repo.claim("evt_1", max_attempts=5) is None


def test_claim_surfaces_database_errors(repo):
    repo.client = MagicMock()
    repo.client.rpc.return_value.execute.side_effect = Exception("function missing")
    with pytest.raises(DatabaseException):
        repo.claim("evt_1", max_attempts=5)


def test_result_writes_require_the_owner_token(repo):
    query = FakeQuery([{"id": "evt_1"}], [])
    repo.table.update.return_value = query
    repo.mark_processed("evt_1", token="tok-1")
    assert ("eq", ("claimed_by", "tok-1")) in query.calls


def test_exhausted_event_is_parked_as_dead(repo):
    repo.get_by_id = MagicMock(return_value=MagicMock(attempts=5))
    query = FakeQuery([{"id": "evt_1"}], [])
    repo.table.update.return_value = query
    status = repo.mark_failed("evt_1", token="t", error="boom", max_attempts=5, backoff_seconds=300)
    assert status == "dead"


def test_failure_below_the_limit_schedules_another_attempt(repo):
    repo.get_by_id = MagicMock(return_value=MagicMock(attempts=2))
    query = FakeQuery([{"id": "evt_1"}], [])
    repo.table.update.return_value = query
    assert repo.mark_failed("evt_1", token="t", error="boom", max_attempts=5, backoff_seconds=300) == "failed"
    scheduled = repo.table.update.call_args[0][0]["next_attempt_at"]
    assert datetime.fromisoformat(scheduled) > datetime.now(tz=timezone.utc)


def test_retryable_selection_filters_in_the_database(repo):
    query = FakeQuery([], [])
    repo.table.select.return_value = query
    repo.list_retryable(max_attempts=5, limit=20)
    names = {c[0] for c in query.calls}
    assert {"in_", "lt", "lte", "order", "limit"} <= names
    assert ("lt", ("attempts", 5)) in query.calls  # attempts filtered before the limit, not after


# --- processing -------------------------------------------------------------------------------

def test_successful_event_is_marked_processed(service):
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data", return_value="ok"):
        assert service.process_stripe_event("evt_1", _event()) == "ok"
    service._CallbackService__stripe_event_repo.mark_processed.assert_called_once()


def test_handler_that_returns_an_exception_is_treated_as_failure(service):
    # buy_bundle/top_up_bundle return the exception object instead of raising it.
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__stripe_event_repo.mark_failed.return_value = "failed"
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value=Exception("hub refused")):
        assert service.process_stripe_event("evt_1", _event()) is None
    service._CallbackService__stripe_event_repo.mark_processed.assert_not_called()
    assert "hub refused" in service._CallbackService__stripe_event_repo.mark_failed.call_args.kwargs["error"]


def test_raising_handler_is_recorded_as_failure(service):
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__stripe_event_repo.mark_failed.return_value = "failed"
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      side_effect=Exception("hub down")):
        assert service.process_stripe_event("evt_1", _event()) is None
    service._CallbackService__stripe_event_repo.mark_processed.assert_not_called()


def test_event_held_by_another_worker_is_not_handled(service):
    service._CallbackService__stripe_event_repo.claim.return_value = None
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data") as handler:
        assert service.process_stripe_event("evt_1", _event()) is None
        handler.assert_not_called()


# --- retry ------------------------------------------------------------------------------------

def test_retry_replays_a_pending_event_from_the_stored_payload(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    service._CallbackService__stripe_event_repo.payload_of.return_value = (json.dumps(_event()), None)
    with patch("app.services.callback_service.stripe.Event.retrieve") as fetch, \
            patch.object(CallbackService, "process_stripe_event", return_value=None) as process:
        assert service.retry_stripe_events() == 1
        fetch.assert_not_called()  # no dependency on Stripe's 30-day retention
        assert process.call_args.kwargs["event"]["id"] == "evt_1"


def test_retry_falls_back_to_stripe_when_no_payload_was_stored(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    service._CallbackService__stripe_event_repo.payload_of.return_value = (None, None)
    with patch("app.services.callback_service.stripe.Event.retrieve", return_value=_event()) as fetch, \
            patch.object(CallbackService, "process_stripe_event", return_value=None):
        assert service.retry_stripe_events() == 1
        fetch.assert_called_once_with("evt_9")


def test_retry_skips_an_event_it_cannot_rebuild(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    service._CallbackService__stripe_event_repo.payload_of.return_value = (None, None)
    with patch("app.services.callback_service.stripe.Event.retrieve", side_effect=Exception("gone")), \
            patch.object(CallbackService, "process_stripe_event") as process:
        assert service.retry_stripe_events() == 0
        process.assert_not_called()


def test_retry_does_nothing_when_everything_is_done(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = []
    assert service.retry_stripe_events() == 0


# --- fixes from the second review ---------------------------------------------------------------

def test_conflict_without_an_existing_row_is_not_treated_as_duplicate(repo):
    # A different constraint or trigger can also say "duplicate key"; that must stay retryable.
    repo.table.insert.return_value.execute.side_effect = Exception("duplicate key on some other index")
    repo.table.select.return_value = FakeQuery([], [])
    with pytest.raises(DatabaseException):
        repo.record("evt_1", "payment_intent.succeeded", payload="{}")


def test_abandoned_final_attempt_is_swept_into_dead(service):
    service._CallbackService__stripe_event_repo.list_abandoned.return_value = [MagicMock(id="evt_dead")]
    service._CallbackService__stripe_event_repo.list_retryable.return_value = []
    assert service.retry_stripe_events() == 0
    service._CallbackService__stripe_event_repo.mark_dead.assert_called_once()


def test_declined_payment_is_not_retried(service):
    from fastapi import HTTPException
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__user_order_repo = MagicMock()
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value=HTTPException(status_code=200, detail="Payment Failed")):
        service.process_stripe_event("evt_1", {"id": "evt_1", "type": "payment_intent.payment_failed"})
    service._CallbackService__stripe_event_repo.mark_processed.assert_called_once()
    service._CallbackService__stripe_event_repo.mark_failed.assert_not_called()


def test_first_delivery_of_a_pending_order_still_runs(service):
    from app.models.user import OrderStatusEnum
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_order_repo.get_by_id.return_value = MagicMock(
        payment_status=OrderStatusEnum.PENDING)
    event = {"id": "evt_1", "type": "payment_intent.succeeded",
             "data": {"object": {"metadata": {"order_id": "ord_1"}}}}
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value="ok") as handler:
        assert service.process_stripe_event("evt_1", event) == "ok"
        handler.assert_called_once()


def test_due_rows_cannot_starve_crash_recovery(repo):
    due = [{"id": f"due_{i}", "status": "failed", "attempts": 0} for i in range(20)]
    stale = [{"id": "stale_1", "status": "processing", "attempts": 1}]
    calls = {"n": 0}

    def select(*_args, **_kwargs):
        calls["n"] += 1
        return FakeQuery(due if calls["n"] == 1 else stale, [])

    repo.table.select.side_effect = select
    ids = [e.id for e in repo.list_retryable(max_attempts=5, limit=20)]
    assert "stale_1" in ids and len(ids) == 20


def test_dead_sweep_cannot_overwrite_a_worker_that_finished(repo):
    query = FakeQuery([], [])
    repo.table.update.return_value = query
    assert repo.mark_dead("evt_1", token="tok", error="gone") is False
    assert ("eq", ("status", "processing")) in query.calls
    assert ("eq", ("claimed_by", "tok")) in query.calls


def test_guard_refuses_to_run_fulfilment_when_the_order_cannot_be_read(service):
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__stripe_event_repo.mark_failed.return_value = "failed"
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_order_repo.get_by_id.side_effect = Exception("db down")
    event = {"id": "evt_1", "type": "payment_intent.succeeded",
             "data": {"object": {"metadata": {"order_id": "ord_1"}}}}
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data") as handler:
        assert service.process_stripe_event("evt_1", event) is None
        handler.assert_not_called()  # fail closed: never order blind
    service._CallbackService__stripe_event_repo.mark_processed.assert_not_called()


def test_paid_order_that_monty_never_fulfilled_is_retried(service):
    from app.models.user import OrderStatusEnum
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__user_order_repo = MagicMock()
    # payment succeeded, but the hub call failed: payment_status is success anyway
    service._CallbackService__user_order_repo.get_by_id.return_value = MagicMock(
        payment_status=OrderStatusEnum.SUCCESS, order_status=OrderStatusEnum.FAILURE, esim_order_id=None)
    event = {"id": "evt_1", "type": "payment_intent.succeeded",
             "data": {"object": {"metadata": {"order_id": "ord_1"}}}}
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value="ok") as handler:
        assert service.process_stripe_event("evt_1", event) == "ok"
        handler.assert_called_once()


def test_order_already_delivered_by_monty_is_not_ordered_again(service):
    from app.models.user import OrderStatusEnum
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_order_repo.get_by_id.return_value = MagicMock(
        payment_status=OrderStatusEnum.SUCCESS, order_status=OrderStatusEnum.SUCCESS,
        esim_order_id="hub_123")
    event = {"id": "evt_1", "type": "payment_intent.succeeded",
             "data": {"object": {"metadata": {"order_id": "ord_1"}}}}
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data") as handler:
        service.process_stripe_event("evt_1", event)
        handler.assert_not_called()
    service._CallbackService__stripe_event_repo.mark_processed.assert_called_once()


def test_unrebuildable_event_consumes_an_attempt(service):
    service._CallbackService__stripe_event_repo.list_abandoned.return_value = []
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    service._CallbackService__stripe_event_repo.payload_of.return_value = (None, None)
    service._CallbackService__stripe_event_repo.claim.return_value = "tok"
    with patch("app.services.callback_service.stripe.Event.retrieve", side_effect=Exception("gone")):
        assert service.retry_stripe_events() == 0
    service._CallbackService__stripe_event_repo.mark_failed.assert_called_once()
