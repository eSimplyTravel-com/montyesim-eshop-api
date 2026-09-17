from app.config.email_utils import mailbox_key
from app.schemas.auth import LoginRequest


def test_plus_tag_is_removed():
    assert mailbox_key("Name+Trip@Example.com") == "name@example.com"


def test_gmail_dots_and_googlemail_fold_together():
    assert mailbox_key("n.a.m.e+1@googlemail.com") == mailbox_key("name@gmail.com")


def test_non_gmail_dots_are_kept():
    assert mailbox_key("first.last@example.com") != mailbox_key("firstlast@example.com")


def test_different_mailboxes_differ():
    assert mailbox_key("alice@example.com") != mailbox_key("bob@example.com")


def test_empty_and_malformed_give_empty_key():
    assert mailbox_key(None) == ""
    assert mailbox_key("") == ""
    assert mailbox_key("not-an-email") == ""


def test_login_accepts_plus_alias_and_keeps_it():
    request = LoginRequest(email="name+trip@example.com")
    assert request.email == "name+trip@example.com"
