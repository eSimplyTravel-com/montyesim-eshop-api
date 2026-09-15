from types import SimpleNamespace

from app.config.utils import vat_in_price_cents


def test_no_calculation_means_no_vat():
    assert vat_in_price_cents(None) == 0


def test_exclusive_vat_is_counted():
    tax = SimpleNamespace(tax_amount_exclusive=359, tax_amount_inclusive=0)
    assert vat_in_price_cents(tax) == 359


def test_inclusive_vat_is_counted():
    # tax_behavior "inclusive": Stripe backs VAT out of the price and leaves the
    # exclusive field at 0. Reading only the exclusive field reports no VAT.
    tax = SimpleNamespace(tax_amount_exclusive=0, tax_amount_inclusive=287)
    assert vat_in_price_cents(tax) == 287


def test_missing_or_null_fields_are_zero():
    assert vat_in_price_cents(SimpleNamespace()) == 0
    assert vat_in_price_cents(SimpleNamespace(tax_amount_exclusive=None, tax_amount_inclusive=None)) == 0
