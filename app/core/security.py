"""Password hashing, session tokens, CSRF, and PII encryption.

Fixes from the original study (§14):
* passwords were posted in the clear with unverifiable hashing -> Argon2id.
* ``SESPASS`` kept the user's password in ``$_SESSION`` -> never stored, anywhere.
* no CSRF protection on the AJAX handlers -> per-session CSRF token enforced.
* player PII (address, birthdate, GCash, ID photos) sat in plain columns -> optional
  authenticated encryption at rest with a key held outside the database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2 import exceptions as argon_exc
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_settings = get_settings()

# Argon2id, OWASP-recommended baseline (19 MiB, t=2, p=1) raised a little.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 -> readable on thermal paper


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        # Constant-ish time: still run a hash so timing does not reveal account existence.
        _hasher.hash("stelnexus-timing-equaliser")
        return False
    try:
        return _hasher.verify(encoded, password)
    except (argon_exc.VerifyMismatchError, argon_exc.VerificationError, argon_exc.InvalidHashError):
        return False


def needs_rehash(encoded: str) -> bool:
    try:
        return _hasher.check_needs_rehash(encoded)
    except argon_exc.InvalidHashError:
        return True


COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "qwerty123",
    "admin123", "administrator", "stelnexus", "onlinebukis", "letmein123", "welcome1",
}


def password_problems(password: str, username: str = "", *, min_length: int | None = None) -> list[str]:
    """What is wrong with ``password`` (empty list = fine).

    ``min_length`` is the live "Minimum password length" setting (Admin > Settings > Security). Always pass it
    where a settings store is at hand. Without it the PASSWORD_MIN_LENGTH config default applies; that used to be
    enforced even when the setting said less, so lowering the setting to 8 still demanded 12.
    """
    minimum = _settings.PASSWORD_MIN_LENGTH if min_length is None else int(min_length)
    problems: list[str] = []
    if len(password) < minimum:
        problems.append(f"Use at least {minimum} characters.")
    if len(password) > 256:
        problems.append("Password is too long.")
    if password.lower() in COMMON_PASSWORDS:
        problems.append("That password is too common.")
    if username and username.lower() in password.lower():
        problems.append("Password must not contain your username.")
    classes = sum(
        bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3:
        problems.append("Mix at least three of: lowercase, uppercase, digits, symbols.")
    return problems


# --- opaque tokens ---------------------------------------------------------

def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Store only the digest: a DB leak must not hand over live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def random_code(length: int = 10) -> str:
    """Replacement for the old ``string_randomiser()`` (which used non-CSPRNG rand)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def numeric_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- CSRF ------------------------------------------------------------------

@dataclass(frozen=True)
class CsrfToken:
    value: str

    def __str__(self) -> str:  # so templates can print it directly
        return self.value


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(session_token: str | None, presented: str | None) -> bool:
    if not session_token or not presented:
        return False
    if len(presented) > 200:
        return False
    return constant_time_eq(session_token, presented)


# --- PII encryption --------------------------------------------------------

class PiiCipher:
    """Authenticated encryption for personal data. No-op when PII_KEY is unset."""

    def __init__(self, key: str | None) -> None:
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None or plaintext == "":
            return plaintext
        if not self._fernet:
            return plaintext
        return "enc:v1:" + self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, stored: str | None) -> str | None:
        if not stored or not stored.startswith("enc:v1:"):
            return stored
        if not self._fernet:
            return "••• (locked)"
        try:
            return self._fernet.decrypt(stored[7:].encode()).decode()
        except InvalidToken:
            return "••• (unreadable)"


pii = PiiCipher(_settings.PII_KEY)


def generate_pii_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def mask(value: str | None, keep: int = 4) -> str:
    """Redact PII for logs / list views: 09171234567 -> *******4567."""
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]
