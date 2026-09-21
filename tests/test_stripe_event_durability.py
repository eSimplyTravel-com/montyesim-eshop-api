from unittest.mock import MagicMock, patch

import pytest

from app.repo.stripe_event_repo import StripeEventRepo
from app.services.callback_service import CallbackService


@pytest.fixture
def repo():
    repo = object.__new__(StripeEventRepo)
    repo.table = MagicMock()
    repo.model = MagicMock()
    return repo


@pytest.fixture
def service():
    # __init__ builds live clients; the event bookkeeping is what these tests exercise.
    service = object.__new__(CallbackService)
    service._CallbackService__stripe_event_repo = MagicMock()
    service._CallbackService__task_executor = MagicMock()
    return service


def test_record_returns_false_on_duplicate(repo):
    with patch.object(StripeEventRepo, "create", side_effect=Exception("duplicate key")):
        assert repo.record("evt_1", "payment_intent.succeeded") is False


def test_record_returns_true_for_new_event(repo):
    with patch.object(StripeEventRepo, "create", return_value=MagicMock()):
        assert repo.record("evt_1", "payment_intent.succeeded") is True


def test_claim_is_false_when_another_worker_took_it(repo):
    with patch.object(StripeEventRepo, "update_by", return_value=None):
        assert repo.claim("evt_1") is False


def test_list_retryable_skips_events_over_the_attempt_limit(repo):
    rows = {
        "failed": [MagicMock(id="a", attempts=5), MagicMock(id="b", attempts=1)],
        "pending": [], "processing": [],
    }
    with patch.object(StripeEventRepo, "list", side_effect=lambda where, **kw: rows[where["status"]]):
        assert [e.id for e in repo.list_retryable(max_attempts=5)] == ["b"]


def test_processed_event_is_marked_and_handler_runs(service):
    service._CallbackService__stripe_event_repo.claim.return_value = True
    service._CallbackService__stripe_event_repo.get_attempts.return_value = 0
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value="handled") as handler:
        assert service.process_stripe_event("evt_1", {"id": "evt_1"}) == "handled"
        handler.assert_called_once()
    service._CallbackService__stripe_event_repo.mark_processed.assert_called_once_with("evt_1")
    service._CallbackService__stripe_event_repo.mark_failed.assert_not_called()


def test_failing_handler_is_recorded_not_swallowed(service):
    service._CallbackService__stripe_event_repo.claim.return_value = True
    service._CallbackService__stripe_event_repo.get_attempts.return_value = 2
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      side_effect=Exception("hub down")):
        assert service.process_stripe_event("evt_1", {"id": "evt_1"}) is None
    args = service._CallbackService__stripe_event_repo.mark_failed.call_args.kwargs
    assert args["event_id"] == "evt_1" and args["attempts"] == 3 and "hub down" in args["error"]
    service._CallbackService__stripe_event_repo.mark_processed.assert_not_called()


def test_unclaimed_event_is_not_handled_twice(service):
    service._CallbackService__stripe_event_repo.claim.return_value = False
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data") as handler:
        assert service.process_stripe_event("evt_1", {"id": "evt_1"}) is None
        handler.assert_not_called()


def test_retry_refetches_from_stripe_and_reprocesses(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    with patch("app.services.callback_service.stripe.Event.retrieve", return_value={"id": "evt_9"}) as fetch, \
            patch.object(CallbackService, "process_stripe_event", return_value=None) as process:
        assert service.retry_stripe_events() == 1
        fetch.assert_called_once_with("evt_9")
        assert process.call_args.kwargs["claim_failed"] is True


def test_retry_skips_events_stripe_cannot_return(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = [MagicMock(id="evt_9")]
    with patch("app.services.callback_service.stripe.Event.retrieve", side_effect=Exception("no such event")), \
            patch.object(CallbackService, "process_stripe_event") as process:
        assert service.retry_stripe_events() == 0
        process.assert_not_called()


def test_retry_does_nothing_when_all_events_are_done(service):
    service._CallbackService__stripe_event_repo.list_retryable.return_value = []
    assert service.retry_stripe_events() == 0


def _webhook_request(body=b"{}"):
    request = MagicMock()

    async def body_coro():
        return body

    request.body = body_coro
    request.headers = {"stripe-signature": "sig"}
    return request


def test_duplicate_delivery_is_not_queued_again(service):
    import asyncio
    service._CallbackService__stripe_event_repo.record.return_value = False
    with patch("app.services.callback_service.stripe.Webhook.construct_event",
               return_value={"id": "evt_1", "type": "payment_intent.succeeded"}):
        asyncio.run(service.handle_payment_webhook(_webhook_request()))
    service._CallbackService__task_executor.add_task.assert_not_called()


def test_first_delivery_is_recorded_then_queued(service):
    import asyncio
    service._CallbackService__stripe_event_repo.record.return_value = True
    with patch("app.services.callback_service.stripe.Webhook.construct_event",
               return_value={"id": "evt_2", "type": "payment_intent.succeeded"}):
        asyncio.run(service.handle_payment_webhook(_webhook_request()))
    service._CallbackService__stripe_event_repo.record.assert_called_once()
    service._CallbackService__task_executor.add_task.assert_called_once()
