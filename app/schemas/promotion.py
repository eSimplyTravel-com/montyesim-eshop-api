from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from app.config.db import PromotionRuleAction
from app.schemas.home import BundleDTO


class PromotionCodeDetailsResponse(BaseModel):
    code_type: str
    rule_id: str


class PromotionValidationRequest(BaseModel):
    promo_code: str
    bundle_code: str

    @field_validator("promo_code")
    @classmethod
    def normalize_promo_code(cls, value: str) -> str:
        # Same normalisation as AssignRequest: storage is uppercase, lookup is
        # byte-exact, and clients disagree about keyboard casing.
        return value.strip().upper()


class PromotionCheck(BaseModel):
    amount: float
    message: str
    type: Optional[int] = PromotionRuleAction.DISCOUNT_AMOUNT.value


class ReferralRewardRequest(BaseModel):
    referral_code: str
    bundle_code: str


class PromotionHistoryDto(BaseModel):
    is_referral: bool
    amount: str
    name: Optional[str]
    promotion_name: Optional[str]
    date: str

    @field_validator("date", mode="before")
    @classmethod
    def convert_to_timestamp(cls, value):
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        return str(int(dt.timestamp()))


class PromotionValidationResponse(BaseModel):
    bundle: BundleDTO
    message: str
    rule_id: str


class ReferralInfoDto(BaseModel):
    amount: float
    currency: str
    type: str
    message: str
