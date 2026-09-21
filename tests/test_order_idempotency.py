import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.db import OrderStatusEnum
from app.exceptions import FulfilmentNeedsReviewError
from app.services.bundle_service import BundleService
from app.services.callback_service import CallbackService


@pytest.fixture
def bundle_service():
    # __init__ builds live clients; these tests only exercise the fulfilment guards.
    service = object.__new__(BundleService)
    service._BundleService__esim_hub_service = AsyncMock()
    service._BundleService__user_order_repo = MagicMock()
    service._BundleService__user_profile_repo = MagicMock()
    service._BundleService__user_profile_bundle_repo = MagicMock()
    service._BundleService__user_repo = MagicMock()
    service._BundleService__currency_service = MagicMock()
    service._BundleService__promotion_service = AsyncMock()
    service._BundleService__task_executor = MagicMock()
    service._BundleService__user_repo.get_by_id.return_value = MagicMock(metadata={}, email="a@b.c")
    service._BundleService__currency_service.get_currency_rate.return_value = 1.0
    return service


def _order(**kwargs):
    defaults = dict(id="ord_1", user_id="usr_1", currency="USD", modified_amount=131, amount=131,
                    promo_code=None, referral_code=None, esim_order_id=None,
                    order_status=OrderStatusEnum.PENDING, searched_countries=None)
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _buy(service, order, bundle=None):
    with patch.object(BundleService, "_BundleService__bundle_type", return_value="COUNTRY"), \
            patch.object(BundleService, "_BundleService__get_discount_amount", return_value=None), \
            patch.object(BundleService, "_BundleService__get_discount_rate", return_value=None):
        return asyncio.run(service.buy_bundle(user_order=order, bundle=bundle or MagicMock(bundle_code="b1"),
                                              user_id="usr_1", payment_status=OrderStatusEnum.SUCCESS,
                                              payment_type="Card"))


def test_already_fulfilled_order_does_not_call_monty_again(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = MagicMock(id="prof_1")
    _buy(bundle_service, _order(esim_order_id="hub_1"))
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_hub_order_without_local_profile_is_parked_for_review(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    with pytest.raises(FulfilmentNeedsReviewError):
        _buy(bundle_service, _order(esim_order_id="hub_1"))
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_interrupted_fulfilment_is_never_ordered_again(bundle_service):
    # A previous attempt reached Monty and never came back: ordering again could buy a second eSIM.
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    with pytest.raises(FulfilmentNeedsReviewError):
        _buy(bundle_service, _order(order_status=OrderStatusEnum.FULFILLING))
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_first_attempt_marks_fulfilling_before_calling_monty(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    bundle_service._BundleService__esim_hub_service.create_reseller_order.return_value = None
    _buy(bundle_service, _order())
    marks = [c.kwargs.get("data", {}) for c in bundle_service._BundleService__user_order_repo.update_by.call_args_list]
    positional = [c.args[1] if len(c.args) > 1 else {} for c in
                  bundle_service._BundleService__user_order_repo.update_by.call_args_list]
    written = [d.get("order_status") for d in marks + positional if isinstance(d, dict)]
    assert OrderStatusEnum.FULFILLING in written


def test_hub_order_id_is_saved_before_the_local_records(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    hub_order = MagicMock(orderId="hub_9", iccid="ic", validityData="v", smdpAdress="s",
                          activationCode="a", allowTopup=True)
    bundle_service._BundleService__esim_hub_service.create_reseller_order.return_value = hub_order
    bundle_service._BundleService__user_profile_repo.create.side_effect = Exception("db down")
    with patch.object(BundleService, "_BundleService__send_email"), \
            patch.object(BundleService, "_BundleService__send_buy_notification", new=AsyncMock()):
        with pytest.raises(Exception):
            _buy(bundle_service, _order())
    saved = [c for c in bundle_service._BundleService__user_order_repo.update_by.call_args_list
             if "hub_9" in str(c)]
    assert saved, "the hub order id must be persisted before anything else can fail"


# --- the event side ---------------------------------------------------------------------------

@pytest.fixture
def callback():
    service = object.__new__(CallbackService)
    service._CallbackService__stripe_event_repo = MagicMock()
    service._CallbackService__user_order_repo = MagicMock()
    service._CallbackService__user_profile_repo = MagicMock()
    return service


def _event():
    return {"id": "evt_1", "type": "payment_intent.succeeded",
            "data": {"object": {"metadata": {"order_id": "ord_1"}}}}


def test_order_needing_review_is_parked_not_retried(callback):
    callback._CallbackService__stripe_event_repo.claim.return_value = "tok"
    callback._CallbackService__user_order_repo.get_by_id.return_value = _order()
    callback._CallbackService__user_profile_repo.get_first_by.return_value = None
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      side_effect=FulfilmentNeedsReviewError("unknown outcome")):
        assert callback.process_stripe_event("evt_1", _event()) is None
    callback._CallbackService__stripe_event_repo.mark_dead.assert_called_once()
    callback._CallbackService__stripe_event_repo.mark_failed.assert_not_called()


def test_done_requires_the_local_profile_as_well(callback):
    callback._CallbackService__stripe_event_repo.claim.return_value = "tok"
    callback._CallbackService__user_order_repo.get_by_id.return_value = _order(esim_order_id="hub_1")
    callback._CallbackService__user_profile_repo.get_first_by.return_value = None  # records missing
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data",
                      return_value="ok") as handler:
        callback.process_stripe_event("evt_1", _event())
        handler.assert_called_once()  # not "done": the repair path must run


def test_fully_fulfilled_order_is_recorded_without_rerunning(callback):
    callback._CallbackService__stripe_event_repo.claim.return_value = "tok"
    callback._CallbackService__user_order_repo.get_by_id.return_value = _order(esim_order_id="hub_1")
    callback._CallbackService__user_profile_repo.get_first_by.return_value = MagicMock(id="prof_1")
    with patch.object(CallbackService, "_CallbackService__handle_payment_webhook_data") as handler:
        callback.process_stripe_event("evt_1", _event())
        handler.assert_not_called()
    callback._CallbackService__stripe_event_repo.mark_processed.assert_called_once()
