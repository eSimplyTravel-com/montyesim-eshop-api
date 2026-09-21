import os
from app.config.db import ConfigKeysEnum


def get_config(key: ConfigKeysEnum | str, default_value: str | int | float | None = None) -> str | None:
    from app.models.app import AppConfigModel
    from app.repo.config_repo import ConfigRepo
    config_repo = ConfigRepo()
    key = key.value if isinstance(key, ConfigKeysEnum) else key
    val: AppConfigModel = config_repo.get_first_by(where={"key": key})
    if val is None:
        os_val = os.getenv(str(key), default_value)
        if os_val:
            config_repo.create({"key": key, "value": os_val})
        return os_val
    return val.value

def wallet_payments_enabled() -> bool:
    """True only if ALLOWED_PAYMENT_TYPES in app_config explicitly lists Wallet.

    Read-only on purpose: get_config() writes a row back when the key is missing, and the live
    row is stored lowercase ("allowed_payment_types"), so an exact-match lookup on the uppercase
    name would insert a duplicate that the app could pick up. A missing row means disabled.
    """
    from app.repo.config_repo import ConfigRepo
    for config in ConfigRepo().list(where={}):
        if (config.key or "").upper() == "ALLOWED_PAYMENT_TYPES":
            return "WALLET" in [item.strip().upper() for item in (config.value or "").split(",")]
    return False


def fake_payment_webhook_enabled() -> bool:
    """The fake-payment endpoint runs the full payment handler on an unsigned, unauthenticated
    body. It stays off unless someone deliberately switches it on for a test environment."""
    return os.getenv("ENABLE_FAKE_PAYMENT_WEBHOOK", "false").strip().lower() in ("true", "1", "yes")
