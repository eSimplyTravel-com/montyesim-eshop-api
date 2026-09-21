from enum import StrEnum, IntEnum


class ConfigKeyEnum(StrEnum):
    KEY_VERSION = "KEY_VERSION"
    KEY_CURRENCY = "KEY_CURRENCY"


class OrderStatusEnum(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    CANCELED = "canceled"
    # Set before the eSIM Hub call so a replay can tell "never ordered" from "outcome unknown".
    FULFILLING = "fulfilling"


class UserOrderType(StrEnum):
    ASSIGN = "Assign"
    BUNDLE_TOP_UP = "Topup"
    WALLET_TOP_UP = "Wallet_Top_Up"


class PaymentTypeEnum(StrEnum):
    CARD = "Card"
    WALLET = "Wallet"
    DCB = "DCB"


class PromotionStatusEnum(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING = "pending"


class UserBundleType(StrEnum):
    PRIMARY_BUNDLE = "Primary Bundle"
    TOP_UP_BUNDLE = "TopUp Bundle"


class NotificationCategoryType(StrEnum):
    BUNDLE_STARTED = "BUNDLE_STARTED"
    CONSUMPTION80 = "BUNDLE_SIGNIFICANT_CONSUMPTION"
    CONSUMPTION100 = "BUNDLE_EXPIRED"


class PromotionRuleAction(IntEnum):
    DISCOUNT_AMOUNT = 1
    DISCOUNT_PERCENTAGE = 2
    CASHBACK_AMOUNT = 3
    CASHBACK_PERCENTAGE = 4


class PromotionRuleEvent(IntEnum):
    CREATE_ORDER = 1
    CREATE_ACCOUNT = 2


class Beneficiary(IntEnum):
    REFERRED = 0
    REFERRER = 1
    BOTH = 2


class ConfigKeysEnum(StrEnum):
    APP_CACHE_KEY = "APP_CACHE_KEY"
    DEFAULT_REFERRAL_RULE_ID = "DEFAULT_REFERRAL_RULE_ID"
    REFERRAL_CODE_AMOUNT = "REFERRAL_CODE_AMOUNT"
    REFERRAL_CODE_PERCENTAGE = "REFERRAL_CODE_PERCENTAGE"
    DEFAULT_CURRENCY = "DEFAULT_CURRENCY"
    DEFAULT_PAYMENT_METHODS = "DEFAULT_PAYMENT_METHODS"
    STRIPE_TAX_CODE = "STRIPE_TAX_CODE"
    STRIPE_TAX_BEHAVIOR = "STRIPE_TAX_BEHAVIOR"
    OTP_EXPIRATION_TIME = "OTP_EXPIRATION_TIME"
    LOGIN_TYPE = "login_type"


class DatabaseTables(StrEnum):
    TABLE_DEVICE = "device"
    TABLE_CONTACT_US = "contact_us"
    TABLE_NOTIFICATION = "notification"
    TABLE_APP_CONFIG = "app_config"
    TABLE_STRIPE_EVENT = "stripe_event"
    TABLE_BANNER = "banner"

    TABLE_PROMOTION_RULE_ACTION = "promotion_rule_action"
    TABLE_PROMOTION_RULE_EVENT = "promotion_rule_event"
    TABLE_PROMOTION_RULE = "promotion_rule"
    TABLE_PROMOTION = "promotion"
    TABLE_PROMOTION_USAGE = "promotion_usage"

    TABLE_USER_OTP = "user_otp"
    TABLE_USER_WALLET = "user_wallet"
    TABLE_USER_WALLET_TRANSACTION = "user_wallet_transaction"
    TABLE_USER_ORDER = "user_order"
    TABLE_USER_PROFILE_BUNDLE = "user_profile_bundle"
    TABLE_USER_PROFILE = "user_profile"
    TABLE_USER_COPY = "users_copy"

    TABLE_BUNDLE = "bundle"
    TABLE_BUNDLE_TRANSLATION = "bundle_translation"
    TABLE_TAG = "tag"
    TABLE_TAG_TRANSLATION = "tag_translation"
    TABLE_BUNDLE_TAG = "bundle_tag"
    TABLE_TAG_GROUP = "tag_group"

    TABLE_CURRENCY = "currency"

    TABLE_VOUCHER = "voucher"
