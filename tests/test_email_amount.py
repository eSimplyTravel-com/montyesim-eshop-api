from decimal import Decimal

from app.config.utils import usd_cents_to_amount


def test_matches_charge_rounding_instead_of_truncating():
    # 142 USD cents * 0.92218 = 130.95 cents: the charge rounds to 1.31, truncation gave 1.30.
    assert usd_cents_to_amount(142, 0.92218) == Decimal("1.31")


def test_half_cent_rounds_up_like_stripe_amount():
    assert usd_cents_to_amount(100, 1.005) == Decimal("1.01")


def test_same_result_as_charge_formula():
    from decimal import ROUND_HALF_UP
    for cents, rate in [(142, 0.92218), (1436, 0.8671), (999, 1.0), (1, 0.5)]:
        charge_cents = (Decimal(str(cents)) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        assert usd_cents_to_amount(cents, rate) == charge_cents / 100


def test_accepts_float_cents_from_background_update():
    assert usd_cents_to_amount(142.0, 0.92218) == Decimal("1.31")
