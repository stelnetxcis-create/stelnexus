"""Rules every new account must pass, in one place: username, Philippine mobile number, Gmail.

Pure functions (no database); the "is it already used?" checks live in services/identity.py.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from app.core.config import get_settings
from app.core.errors import ValidationError

DEFAULT_USERNAME_MIN = 7
USERNAME_MAX = 32
_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$|^[A-Za-z0-9]$")
_DOUBLE_SPECIAL = re.compile(r"[._-]{2,}")
# Names a stranger could use to pass as staff or the house. Blocked on public sign-up only (the Admin can still create one).
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "support", "helpdesk", "staff", "system", "moderator", "official", "security",
    "billing", "cashier", "teller", "declarator", "master", "agent", "subagent", "bishop", "king", "pawn", "house",
    "stelnexus", "pcso", "anonymous", "null", "undefined",
})


def check_username(value: str | None, *, min_length: int = DEFAULT_USERNAME_MIN, public_signup: bool = False) -> str:
    """The cleaned username, or a ValidationError that says exactly what to change. (Uniqueness is checked elsewhere.)"""
    value = (value or "").strip()
    n = int(min_length)
    if len(value) < n:
        raise ValidationError(f"Username must be at least {n} characters (you have {len(value)}).")
    if len(value) > USERNAME_MAX:
        raise ValidationError(f"Username can be at most {USERNAME_MAX} characters.")
    if not _SHAPE.match(value):
        raise ValidationError("Username may use letters, numbers, dot, dash and underscore only, and must start and end "
                              "with a letter or number.")
    if _DOUBLE_SPECIAL.search(value):
        raise ValidationError("Username cannot have two dots, dashes or underscores in a row.")
    if public_signup and re.sub(r"[._-]", "", value).lower() in RESERVED_USERNAMES:
        raise ValidationError("That username is reserved. Please choose another.")
    return value


# ------------------------------------------------------------------------------------------------ Philippine mobile
_PH_MOBILE = re.compile(r"^09\d{9}$")


def normalize_ph_mobile(raw: str | None, *, what: str = "Mobile number") -> str:
    """09XXXXXXXXX. Accepts 09.., +639.., 639.. and 9XXXXXXXXX (spaces, dashes and brackets ignored). Refuses numbers
    that cannot be real: not 11 digits, or an obviously made-up pattern such as 09111111111 or 09123456789."""
    s = re.sub(r"[\s\-()]", "", raw or "")
    if s.startswith("+63"):
        s = "0" + s[3:]
    elif s.startswith("63") and len(s) == 12:
        s = "0" + s[2:]
    elif len(s) == 10 and s.startswith("9"):
        s = "0" + s
    if not _PH_MOBILE.match(s):
        raise ValidationError(f"{what} must be a Philippine mobile number: 11 digits starting with 09 (for example 09171234567).")
    sub = s[2:]
    steps = {(int(b) - int(a)) % 10 for a, b in zip(sub, sub[1:], strict=False)}
    if len(set(sub)) <= 2 or steps <= {1} or steps <= {9}:      # 1-2 different digits, or 0123.. / ..3210 in a row
        raise ValidationError(f"{what} does not look like a real number. Please check it.")
    return s


# ------------------------------------------------------------------------------------------------ Gmail
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
_GMAIL_LOCAL = re.compile(r"^[a-z0-9][a-z0-9.]*$")


def gmail_parts(raw: str | None) -> tuple[str, str]:
    """(address to show, canonical form). Gmail ignores dots and anything after a plus, so ``J.Doe+x@gmail.com`` and
    ``jdoe@googlemail.com`` are the SAME mailbox: the canonical form (``jdoe@gmail.com``) is what is checked for reuse."""
    text = (raw or "").strip().lower()
    if text.count("@") != 1 or " " in text:
        raise ValidationError("Enter your Gmail address, for example juan.delacruz@gmail.com.")
    local, domain = text.split("@")
    if domain not in _GMAIL_DOMAINS:
        raise ValidationError("Only a Gmail address (ending in @gmail.com) can be used here.")
    base = local.split("+", 1)[0]
    if not base or not _GMAIL_LOCAL.match(base) or ".." in base or base.endswith("."):
        raise ValidationError("That does not look like a valid Gmail address.")
    stripped = base.replace(".", "")
    if not 6 <= len(stripped) <= 30:
        raise ValidationError("A Gmail address has 6 to 30 letters or numbers before the @. Please check it.")
    return text, f"{stripped}@gmail.com"


def email_key(canonical: str) -> str:
    """What the database stores to spot a reused Gmail: a keyed hash, so a leaked table does not list addresses."""
    s = get_settings()
    secret = (s.PII_KEY or s.SECRET_KEY).encode()
    return hmac.new(hashlib.sha256(b"email-key:" + secret).digest(), canonical.encode(), hashlib.sha256).hexdigest()
