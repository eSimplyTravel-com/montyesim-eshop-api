import os
from unittest.mock import patch

from app.config.helper import fake_payment_webhook_enabled


def _with_env(value):
    env = {k: v for k, v in os.environ.items() if k != "ENABLE_FAKE_PAYMENT_WEBHOOK"}
    if value is not None:
        env["ENABLE_FAKE_PAYMENT_WEBHOOK"] = value
    return patch.dict(os.environ, env, clear=True)


def test_fake_payment_webhook_is_off_when_unset():
    with _with_env(None):
        assert fake_payment_webhook_enabled() is False


def test_fake_payment_webhook_stays_off_for_anything_but_a_deliberate_yes():
    for value in ("false", "0", "no", "", "maybe", "False "):
        with _with_env(value):
            assert fake_payment_webhook_enabled() is False, value


def test_fake_payment_webhook_can_be_switched_on():
    for value in ("true", "TRUE", " yes", "1"):
        with _with_env(value):
            assert fake_payment_webhook_enabled() is True, value


def test_route_is_guarded_by_the_flag():
    source = open("app/api/v1/callback.py").read()
    guard = source.index("fake_payment_webhook_enabled()")
    handler = source.index("handle_payment_webhook_fake(request)")
    assert guard < handler, "the flag must be checked before the handler runs"
