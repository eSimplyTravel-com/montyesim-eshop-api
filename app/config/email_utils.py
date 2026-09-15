GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def mailbox_key(email: str | None) -> str:
    """Abuse-control key for an email: the mailbox it most likely delivers to.

    Lowercased, "+tag" removed, and for Gmail dots removed and googlemail.com
    folded into gmail.com. Use it ONLY to compare two addresses for abuse checks
    (e.g. self-referral via aliases). Never use it as the account identity or the
    delivery address: not every provider treats "+" as an alias. Mirrors
    normalizeEmail in web-next/lib/fraud-guards.ts.
    """
    if not email or "@" not in email:
        return ""
    local, _, domain = email.strip().lower().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"
