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

def _claim_repo(repo, rows_per_update, current_attempts=0):
    repo.get_by_id = MagicMock(return_value=MagicMock(attempts=current_attempts))
    queries = []

    def update(data):
        query = FakeQuery(rows_per_update.pop(0) if rows_per_update else None, [])
        query.data_sent = data
        queries.append(query)
        return query

    repo.table.update.side_effect = update
    return queries


def test_claim_takes_a_pending_event_and_counts_the_attempt(repo):
    queries = _claim_repo(repo, [[{"id": "evt_1"}]], current_attempts=2)
    token = repo.claim("evt_1")
    assert token
    assert queries[0].data_sent["attempts"] == 3
    assert queries[0].data_sent["claimed_by"] == token


def test_claim_refuses_an_event_another_worker_is_running(repo):
    # pending -> no rows, failed -> no rows, expired processing -> no rows
    _claim_repo(repo, [None, None, None])
    assert repo.claim("evt_1") is None


def test_claim_reclaims_an_event_whose_lease_expired(repo):
    queries = _claim_repo(repo, [None, None, [{"id": "evt_1"}]])
    assert repo.claim("evt_1")
    cutoff = [c for c in queries[2].calls if c[0] == "lt"][0][1]
    assert cutoff[0] == "claimed_at"
    age = datetime.now(tz=timezone.utc) - datetime.fromisoformat(cutoff[1])
    assert timedelta(seconds=LEASE_SECONDS - 60) < age < timedelta(seconds=LEASE_SECONDS + 60)


def test_claim_returns_none_for_an_unknown_event(repo):
    repo.get_by_id = MagicMock(return_value=None)
    assert repo.claim("evt_missing") is None


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
