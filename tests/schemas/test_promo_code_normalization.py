"""Promo codes are stored uppercase and looked up byte-exact, so every request
schema must normalise (trim + uppercase) before the service layer sees the code.
These tests pin that contract for both entry points."""
import pytest

from app.schemas.bundle import AssignRequest
from app.schemas.promotion import PromotionValidationRequest


def _assign(promo):
    return AssignRequest(bundle_code="b1", related_search=None, promo_code=promo,
                         affiliate_code=None)


class TestAssignRequestPromoNormalization:
    def test_lowercase_is_uppercased(self):
        assert _assign("openingoffer10").promo_code == "OPENINGOFFER10"

    def test_whitespace_is_stripped(self):
        assert _assign("  OPENINGOFFER10  ").promo_code == "OPENINGOFFER10"

    def test_mixed_case_and_whitespace(self):
        assert _assign(" OpeningOffer10\n").promo_code == "OPENINGOFFER10"

    def test_none_stays_none(self):
        assert _assign(None).promo_code is None

    def test_whitespace_only_becomes_none(self):
        assert _assign("   ").promo_code is None


class TestValidationRequestPromoNormalization:
    def test_lowercase_is_uppercased(self):
        req = PromotionValidationRequest(promo_code="openingoffer10", bundle_code="b1")
        assert req.promo_code == "OPENINGOFFER10"

    def test_whitespace_is_stripped(self):
        req = PromotionValidationRequest(promo_code=" OPENINGOFFER10 ", bundle_code="b1")
        assert req.promo_code == "OPENINGOFFER10"
