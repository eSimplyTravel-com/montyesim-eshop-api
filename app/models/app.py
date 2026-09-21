import json
from typing import Optional, Dict, Any

from pydantic import BaseModel, Field, field_validator

from app.config.constants import ErrorMessages


class AppConfigModel(BaseModel):
    id: Optional[int] = None
    key: str = Field(None, title="key")
    value: str = Field(None, title="value")


class StripeEventModel(BaseModel):
    id: str
    type: Optional[str] = None
    status: Optional[str] = None
    attempts: Optional[int] = 0
    last_error: Optional[str] = None
    created_at: Optional[str] = None
    processed_at: Optional[str] = None


class ContactUsModel(BaseModel):
    id: Optional[int] = None
    email: str
    content: str
    created_at: Optional[str] = None


class DeviceBase(BaseModel):
    fcm_token: Optional[str] = Field(None, alias="fcm_token")
    manufacturer: Optional[str] = Field(None, alias="manufacturer")
    device_model: Optional[str] = Field(None, alias="device_model")
    os: Optional[str] = Field(None, alias="os")
    os_version: Optional[str] = Field(None, alias="os_version")
    app_version: Optional[str] = Field(None, alias="app_version")
    ram_size: Optional[str] = Field(None, alias="ram_size")
    screen_resolution: Optional[str] = Field(None, alias="screen_resolution")
    is_rooted: Optional[bool] = Field(None, alias="is_rooted")


class DeviceModel(DeviceBase):
    device_id: Optional[str] = Field(None, alias="device_id")
    user_id: Optional[str] = Field(None, alias="user_id")
    is_logged_in: Optional[bool] = Field(None, alias="is_logged_in")
    originated_ip: Optional[str] = Field(None, alias="originated_ip")
    ip_location: Optional[str] = Field(None, alias="ip_location")
    timestamp_login: Optional[str] = Field(None, alias="timestamp_login")
    timestamp_logout: Optional[str] = Field(None, alias="timestamp_logout")


class BundleModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    data: Optional[Dict[str, Any]] = Field(None, alias="data")
    is_active: Optional[bool] = Field(None, alias="is_active")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None

    @field_validator("data", mode="before")
    @classmethod
    def parse_data(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(ErrorMessages.INVALID_JSON_DATA)
        return value


class BundleTranslationModel(BundleModel):
    id: Optional[int] = Field(None, alias="id")
    bundle_id: Optional[str] = Field(None, alias="bundle_id")
    locale: Optional[str] = Field(None, alias="locale")
    data: Optional[Dict[str, Any]] = Field(None, alias="data")


class TagModel(BaseModel):
    id: Optional[str] = Field(None, alias="id")
    tag_group_id: Optional[int] = Field(None, alias="tag_group_id")
    name: Optional[str] = Field(None, alias="name")
    icon: Optional[str] = Field(None, alias="icon")
    data: Optional[Dict[str, Any]] = Field(None, alias="data")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None

    @field_validator("data", mode="before")
    @classmethod
    def parse_data(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON format in bundle_data")
        return value


class TagTranslationModel(BaseModel):
    id: Optional[int] = Field(None, alias="id")
    tag_id: Optional[str] = Field(None, alias="tag_id")
    locale: Optional[str] = Field(None, alias="locale")
    name: Optional[str] = Field(None, alias="name")
    data: Optional[Dict[str, Any]] = Field(None, alias="data")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None

    @field_validator("data", mode="before")
    @classmethod
    def parse_data(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("Invalid JSON format in bundle_data")
        return value


class BundleTagModel(BaseModel):
    id: Optional[int] = Field(None, alias="id")
    bundle_id: Optional[str] = Field(None, alias="bundle_id")
    tag_id: Optional[str] = Field(None, alias="tag_id")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None


class TagGroupModel(BaseModel):
    id: Optional[int] = Field(None, alias="id")
    name: Optional[str] = Field(None, alias="name")
    type: Optional[int] = Field(None, alias="type")
    is_active: Optional[bool] = Field(True, alias="is_active")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None


class CurrencyModel(BaseModel):
    id: Optional[int] = Field(None, alias="id")
    name: Optional[str] = Field(None, alias="name")
    default_currency: Optional[str] = Field(None, alias="default_currency")
    rate: Optional[float] = Field(True, alias="rate")
    updated_at: Optional[str] = None
    created_at: Optional[str] = None


class BannerModel(BaseModel):
    id: int
    title: str
    description: str
    image: str
    action: str
    platform: str
    created_at: str


class UserOtpModel(BaseModel):
    id: Optional[int] = Field(None, alias="id")
    email: Optional[str] = Field(None, alias="email")
    mobile: Optional[str] = Field(None, alias="mobile")
    otp: Optional[str] = Field(None, alias="otp")
    expire_at: Optional[str] = Field(None, alias="expire_at")
    is_used: Optional[bool] = Field(False, alias="is_used")
