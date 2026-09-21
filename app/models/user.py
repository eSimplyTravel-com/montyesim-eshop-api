import json
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.config.db import UserOrderType, OrderStatusEnum, UserBundleType, PaymentTypeEnum


class UserModel(BaseModel):
    id: str
    email: str
    token: str
    msisdn: Optional[str]
    is_verified: bool
    language: Optional[str] = "en"
    is_anonymous: Optional[bool] = False
    anonymous_user_id: Optional[str] = None


class UsersCopyModel(BaseModel):
    id: str
    email: Optional[str]
    metadata: Optional[Dict[str, Any]] = None


class UserOrderModel(BaseModel):
    id: Optional[str] = None
    user_id: str
    esim_order_id: Optional[str] = None
    bundle_id: Optional[str] = None
    order_type: UserOrderType = UserOrderType.ASSIGN
    amount: float
    currency: str
    payment_intent_code: Optional[str] = None
    payment_status: Optional[str] = OrderStatusEnum.PENDING
    order_status: Optional[str] = OrderStatusEnum.PENDING
    payment_time: Optional[str] = None
    bundle_data: Optional[str] = None
    searched_countries: Optional[str] = None
    anonymous_user_id: Optional[str] = None
    created_at: Optional[str] = None
    callback_time: Optional[str] = None
    promo_code: Optional[str] = None
    referral_code: Optional[str] = None
    modified_amount: Optional[float] = 0
    otp: Optional[str] = None
    payment_type: Optional[PaymentTypeEnum] = PaymentTypeEnum.CARD
    otp_expired_at: Optional[str] = None
    # Written only by the claim_order_fulfilment() lock. Declared here because UserOrderModel
    # forbids extra fields: without it, adding the column breaks every order read.
    fulfilment_claimed_at: Optional[str] = None
    tax_amount: Optional[float] = 0.0

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class UserBundleModel(BaseModel):
    id: int
    user_id: str
    bundle_id: str
    created_at: str
    label: Optional[str] = None
    user_order_id: Optional[str] = None
    bundle_data: Optional[str] = None
    searched_countries: Optional[str] = None


class UserProfileBundleModel(BaseModel):
    id: Optional[int]
    user_id: str
    user_order_id: str
    user_profile_id: str
    esim_hub_order_id: str
    iccid: str
    bundle_type: UserBundleType
    plan_started: bool
    bundle_expired: bool
    bundle_data: Optional[Dict[str, Any]] = None
    created_at: Optional[str]

    @field_validator("bundle_data", mode="before")
    @classmethod
    def parse_bundle_data(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)  # Convert string to dictionary
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON format in bundle_data")
        return value  # If already a dict, return as is


class UserProfileModel(BaseModel):
    id: Optional[str]
    user_id: str
    shared_user_id: Optional[str] = None
    user_order_id: str
    iccid: str
    validity: str
    created_at: Optional[str]
    label: Optional[str] = None
    smdp_address: str
    activation_code: Optional[str] = None
    allow_topup: bool
    esim_hub_order_id: str
    searched_countries: Optional[str] = None
    bundles: Optional[List[UserProfileBundleModel]] = Field(None, alias="user_profile_bundle")


class UserProfileBundleWithProfileModel(UserProfileBundleModel):
    user_profile: UserProfileModel


class CallBackNotificationInfoModel(BaseModel):
    user_id: str
    user_display_name: str
    bundle_display_name: str
    iccid: str
    validity: str
    label: Optional[str] = None
    smdp_address: str
    activation_code: str
    allow_topup: bool
    esim_hub_order_id: str
    searched_countries: Optional[str] = None
    bundle: Optional[UserProfileBundleModel] = None


class UserWalletModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    user_id: str = Field(None, alias="user_id")
    amount: float = Field(None, alias="amount")
    currency: str = Field(None, alias="currency")
    created_at: Optional[str] = Field(None, alias="created_at")
    updated_at: Optional[str] = Field(None, alias="updated_at")


class UserWalletTransactionModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    wallet_id: str = Field(None, alias="wallet_id")
    amount: float = Field(None, alias="amount")
    status: str = Field(None, alias="status")
    source: str = Field(None, alias="source")
    created_at: Optional[str] = Field(None, alias="created_at")
