import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.db import OrderStatusEnum
from app.exceptions import EsimHubUnknownOutcomeError, FulfilmentNeedsReviewError
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
    service._BundleService__fulfilment_lock = MagicMock()
    service._BundleService__fulfilment_lock.acquire.return_value = True
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
    bundle_service._BundleService__user_profile_bundle_repo.get_first_by.return_value = MagicMock(id="pb_1")
    _buy(bundle_service, _order(esim_order_id="hub_1"))
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_hub_order_without_local_profile_is_parked_for_review(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    with pytest.raises(FulfilmentNeedsReviewError):
        _buy(bundle_service, _order(esim_order_id="hub_1"))
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_interrupted_fulfilment_is_never_ordered_again(bundle_service):
    # The lock refuses: another worker holds it, or a previous attempt vanished mid-call.
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    bundle_service._BundleService__fulfilment_lock.acquire.return_value = False
    with pytest.raises(FulfilmentNeedsReviewError):
        _buy(bundle_service, _order())
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


def test_the_lock_is_taken_before_monty_is_called(bundle_service):
    order_of_calls = []
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    bundle_service._BundleService__fulfilment_lock.acquire.side_effect = \
        lambda *_a, **_k: order_of_calls.append("lock") or True
    bundle_service._BundleService__esim_hub_service.create_reseller_order.side_effect = \
        lambda *_a, **_k: order_of_calls.append("hub")
    _buy(bundle_service, _order())
    assert order_of_calls == ["lock", "hub"]


def test_unknown_hub_outcome_is_quarantined_not_retried(bundle_service):
    # A timeout may mean the eSIM was bought. Retrying would buy a second one.
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    bundle_service._BundleService__esim_hub_service.create_reseller_order.side_effect = \
        EsimHubUnknownOutcomeError("read timeout")
    with pytest.raises(FulfilmentNeedsReviewError):
        _buy(bundle_service, _order())


def test_explicit_hub_rejection_stays_retryable(bundle_service):
    # The hub answered "no": nothing was created, so this is an ordinary failure.
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = None
    bundle_service._BundleService__esim_hub_service.create_reseller_order.return_value = None
    result = _buy(bundle_service, _order())
    assert not isinstance(result, FulfilmentNeedsReviewError)


def test_missing_plan_record_is_repaired_instead_of_declared_finished(bundle_service):
    bundle_service._BundleService__user_profile_repo.get_first_by.return_value = MagicMock(id="prof_1", iccid="ic")
    bundle_service._BundleService__user_profile_bundle_repo.get_first_by.return_value = None
    _buy(bundle_service, _order(esim_order_id="hub_1"))
    bundle_service._BundleService__user_profile_bundle_repo.create.assert_called_once()
    bundle_service._BundleService__esim_hub_service.create_reseller_order.assert_not_called()


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


# --- top-ups get the same treatment -----------------------------------------------------------

def _top_up(service, order, iccid="ic"):
    with patch.object(BundleService, "_BundleService__bundle_type", return_value="COUNTRY"):
        return asyncio.run(service.top_up_bundle(bundle=MagicMock(bundle_code="b1", bundle_name="B"),
                                                 user_order=order, iccid=iccid, user_id="usr_1",
                                                 payment_status=OrderStatusEnum.SUCCESS))


def test_top_up_already_fulfilled_is_a_no_op(bundle_service):
    bundle_service._BundleService__user_profile_bundle_repo.get_first_by.return_value = MagicMock(id="pb_1")
    _top_up(bundle_service, _order(esim_order_id="hub_top_1"))
    bundle_service._BundleService__esim_hub_service.create_reseller_topup.assert_not_called()


def test_top_up_takes_the_lock_before_calling_monty(bundle_service):
    calls = []
    bundle_service._BundleService__fulfilment_lock.acquire.side_effect = lambda *_a: calls.append("lock") or True
    bundle_service._BundleService__esim_hub_service.create_reseller_topup.side_effect = \
        lambda *_a, **_k: calls.append("hub")
    _top_up(bundle_service, _order())
    assert calls == ["lock", "hub"]


def test_top_up_without_the_lock_is_not_ordered(bundle_service):
    bundle_service._BundleService__fulfilment_lock.acquire.return_value = False
    with pytest.raises(FulfilmentNeedsReviewError):
        _top_up(bundle_service, _order())
    bundle_service._BundleService__esim_hub_service.create_reseller_topup.assert_not_called()


def test_top_up_unknown_outcome_is_quarantined(bundle_service):
    bundle_service._BundleService__esim_hub_service.create_reseller_topup.side_effect = \
        EsimHubUnknownOutcomeError("timeout")
    with pytest.raises(FulfilmentNeedsReviewError):
        _top_up(bundle_service, _order())


def test_successful_top_up_records_its_hub_order_id(bundle_service):
    hub = MagicMock(orderId="hub_top_9")
    bundle_service._BundleService__esim_hub_service.create_reseller_topup.return_value = hub
    bundle_service._BundleService__user_profile_bundle_repo.get_first_by.return_value = MagicMock(bundle_expired=False)
    with patch.object(BundleService, "_BundleService__send_topup_notification", new=AsyncMock()):
        _top_up(bundle_service, _order())
    written = [str(c) for c in bundle_service._BundleService__user_order_repo.update_by.call_args_list]
    assert any("hub_top_9" in c for c in written), "a replay must be able to see that Monty delivered"


# --- the adapter's own outcome contract --------------------------------------------------------

def test_adapter_raises_unknown_outcome_on_transport_failure():
    from app.services.integration.esim_hub_service import EsimHubService
    service = object.__new__(EsimHubService)
    service._EsimHubService__do_request = AsyncMock(side_effect=Exception("read timeout"))
    with pytest.raises(EsimHubUnknownOutcomeError):
        asyncio.run(service.create_reseller_order(bundle_code="b", order_id="o",
                                                  user=MagicMock(metadata={}, email="a@b.c")))


def test_adapter_returns_none_on_an_explicit_rejection():
    from app.services.integration.esim_hub_service import EsimHubService
    service = object.__new__(EsimHubService)
    service._EsimHubService__do_request = AsyncMock(return_value={"success": False, "message": "no stock"})
    assert asyncio.run(service.create_reseller_order(bundle_code="b", order_id="o",
                                                     user=MagicMock(metadata={}, email="a@b.c"))) is None


def test_adapter_raises_unknown_when_the_order_cannot_be_read_back():
    from app.services.integration.esim_hub_service import EsimHubService
    service = object.__new__(EsimHubService)
    service._EsimHubService__do_request = AsyncMock(return_value={"success": True, "data": {"orderId": "hub_1"}})
    service.get_activation_code = AsyncMock(side_effect=Exception("500"))
    with pytest.raises(EsimHubUnknownOutcomeError):
        asyncio.run(service.create_reseller_order(bundle_code="b", order_id="o",
                                                  user=MagicMock(metadata={}, email="a@b.c")))


def test_the_lock_never_expires_on_its_own(bundle_service):
    # A timer that hands a mid-fulfilment order to a second worker is how one payment becomes
    # two eSIMs. The lock asks for no lease at all.
    from app.repo.user_order_repo import OrderFulfilmentLock
    repo = MagicMock()
    OrderFulfilmentLock(repo).acquire("ord_1")
    name, params = repo.client.rpc.call_args[0]
    assert name == "claim_order_fulfilment"
    assert params == {"p_order_id": "ord_1"}, "no lease parameter may be sent"


def test_the_sql_lock_has_no_expiry_clause():
    sql = open("ops/sql/2026-09-21-stripe-event.sql").read()
    claim = sql[sql.index("function public.claim_order_fulfilment"):]
    body = claim[:claim.index("$$;")]
    assert "make_interval" not in body, "the order lock must not release itself on a timer"
    assert "esim_order_id is null" in body
