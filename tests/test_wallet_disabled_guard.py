import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.db import PaymentTypeEnum
from app.config.helper import wallet_payments_enabled
from app.exceptions import CustomException
from app.services.user_service import UserBundleService
from tests.services.test_user_wallet_service import user_wallet_service  # noqa: F401  (fixture)


@pytest.fixture
def user_bundle_service():
    # Bypass __init__ (it builds live Supabase/eSIM Hub clients); the guard runs before anything else.
    service = object.__new__(UserBundleService)
    service._UserBundleService__esim_hub_service = AsyncMock()
    service._UserBundleService__user_order_repo = MagicMock()
    service._UserBundleService__bundle_service = MagicMock()
    return service


def _configs(*rows):
    return [MagicMock(key=key, value=value) for key, value in rows]


@pytest.mark.parametrize("rows, expected", [
    ((("allowed_payment_types", "Card"),), False),  # live row is lowercase
    ((("allowed_payment_types", "Card,Wallet"),), True),
    ((("ALLOWED_PAYMENT_TYPES", " card , wallet "),), True),
    ((("OTHER_KEY", "Wallet"),), False),  # missing row = disabled
    ((), False),
])
def test_wallet_payments_enabled(rows, expected):
    with patch("app.repo.config_repo.ConfigRepo") as repo:
        repo.return_value.list.return_value = _configs(*rows)
        assert wallet_payments_enabled() is expected
        repo.return_value.create.assert_not_called()


def test_wallet_payments_enabled_never_writes_config():
    with patch("app.repo.config_repo.ConfigRepo") as repo:
        repo.return_value.list.return_value = []
        wallet_payments_enabled()
        repo.return_value.create.assert_not_called()


def test_wallet_top_up_refused_when_disabled(user_wallet_service):
    with patch("app.services.user_wallet_service.wallet_payments_enabled", return_value=False):
        with pytest.raises(CustomException):
            user_wallet_service.top_up_wallet(top_up_request=MagicMock(amount=10), user=MagicMock(),
                                              request=MagicMock(), x_currency="EUR")
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.assert_not_called()


def test_wallet_assign_refused_before_any_write(user_bundle_service):
    request = MagicMock(payment_type=PaymentTypeEnum.WALLET)
    with patch("app.services.user_service.wallet_payments_enabled", return_value=False):
        with pytest.raises(CustomException):
            asyncio.run(user_bundle_service.assign(user=MagicMock(), device_id="d", assign_request=request,
                                                   x_currency="EUR", locale="en", request=MagicMock()))
    user_bundle_service._UserBundleService__esim_hub_service.get_bundle_by_id.assert_not_called()
    user_bundle_service._UserBundleService__user_order_repo.create.assert_not_called()


def test_wallet_top_up_bundle_refused_before_order(user_bundle_service):
    request = MagicMock(payment_type=PaymentTypeEnum.WALLET)
    with patch("app.services.user_service.wallet_payments_enabled", return_value=False):
        with pytest.raises(CustomException):
            asyncio.run(user_bundle_service.assign_top_up(user=MagicMock(), assign_top_up_request=request,
                                                          device_id="d", request=MagicMock(),
                                                          x_currency="EUR", locale="en"))
    user_bundle_service._UserBundleService__user_order_repo.create.assert_not_called()


def test_card_assign_does_not_read_wallet_config(user_bundle_service):
    request = MagicMock(payment_type=PaymentTypeEnum.CARD)
    user_bundle_service._UserBundleService__esim_hub_service.get_bundle_by_id.return_value = None
    with patch("app.services.user_service.wallet_payments_enabled") as enabled:
        with pytest.raises(CustomException):  # stops at "bundle not available", past the guard
            asyncio.run(user_bundle_service.assign(user=MagicMock(), device_id="d", assign_request=request,
                                                   x_currency="EUR", locale="en", request=MagicMock()))
        enabled.assert_not_called()
    user_bundle_service._UserBundleService__esim_hub_service.get_bundle_by_id.assert_called_once()


def test_wallet_assign_allowed_when_enabled_reaches_bundle_lookup(user_bundle_service):
    request = MagicMock(payment_type=PaymentTypeEnum.WALLET)
    user_bundle_service._UserBundleService__esim_hub_service.get_bundle_by_id.return_value = None
    with patch("app.services.user_service.wallet_payments_enabled", return_value=True):
        with pytest.raises(CustomException):
            asyncio.run(user_bundle_service.assign(user=MagicMock(), device_id="d", assign_request=request,
                                                   x_currency="EUR", locale="en", request=MagicMock()))
    user_bundle_service._UserBundleService__esim_hub_service.get_bundle_by_id.assert_called_once()
