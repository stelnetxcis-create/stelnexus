"""Environment-driven configuration.

Fixes the original ``db/auth.php`` flaw: database credentials, session keys and
admin domains were hard-coded in a file that shipped inside the project zip.
Nothing secret lives in source here -- everything comes from the environment
(or a git-ignored ``.env``), and production refuses to boot with dev defaults.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]
INSECURE_DEFAULT = "dev-only-insecure-key-change-me-0000000000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- runtime -----------------------------------------------------------
    ENV: Literal["dev", "prod"] = "prod"  # fail-safe: dev must be asked for explicitly
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False

    # --- database (PostgreSQL is the target platform) ----------------------
    DATABASE_URL: str = "postgresql+psycopg://stelnexus:stelnexus@127.0.0.1:5432/stelnexus"
    # Per WORKER. Total connections = (DB_POOL_SIZE + DB_MAX_OVERFLOW) x workers, and PostgreSQL's max_connections
    # (default 100) must be larger than that, or sit behind PgBouncer. See docs/SCALING.md.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False
    # Seconds the dashboard KPIs (open fights, events, downline, cashflow card) may be served from
    # memory. 0 = always live. Staff actions that change them clear the cache immediately.
    DASHBOARD_CACHE_SECONDS: int = Field(default=10, ge=0, le=300)
    # The livestream poll (/_live/event/<id>: pools, live multiples) is built once and shared by every viewer
    # for at most this many seconds. Any commit that touches a ticket, game, event or setting clears it at
    # once, so this is only the safety net for changes made by another process (timers, the CLI). 0 = off.
    LIVE_CACHE_SECONDS: float = Field(default=2.0, ge=0, le=30)
    # Precomputed report figures (flows, top agents) older than this are recomputed inline, so a
    # stopped `refresh-reports` timer degrades to the old behaviour instead of serving stale numbers.
    REPORT_SNAPSHOT_MAX_AGE_SECONDS: int = Field(default=900, ge=0)
    # The full-ledger integrity check is nightly; allow a day plus slack before falling back.
    INTEGRITY_SNAPSHOT_MAX_AGE_SECONDS: int = Field(default=93600, ge=0)

    # --- crypto / sessions -------------------------------------------------
    SECRET_KEY: str = INSECURE_DEFAULT
    PII_KEY: str | None = None  # Fernet key; enables encryption of player PII
    SESSION_COOKIE: str = "stelnexus_sid"
    SESSION_IDLE_MINUTES: int = 30
    SESSION_ABSOLUTE_HOURS: int = 12
    COOKIE_SECURE: bool = True
    # "none" keeps the console usable inside an embedded/preview iframe during
    # development; production is pinned to Lax by the validator below.
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "none"
    FRAME_ANCESTORS: str = "'self' https://*.e2b.app"

    # --- auth hardening ----------------------------------------------------
    MAX_LOGIN_ATTEMPTS: int = 5
    LOCKOUT_MINUTES: int = 15
    PASSWORD_MIN_LENGTH: int = 12
    API_TOKEN_TTL_DAYS: int = 30

    # --- web ---------------------------------------------------------------
    TRUSTED_HOSTS: list[str] = ["*"]
    # Reverse proxies whose X-Forwarded-For is believed (IPs/CIDRs, JSON list in .env). Empty = never.
    TRUSTED_PROXIES: list[str] = []
    # The sandbox/live-preview origin is dynamic; allow it explicitly.
    EXTRA_ORIGINS: list[str] = []
    MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024
    UPLOAD_DIR: Path = BASE_DIR / "var" / "uploads"  # never inside /static

    # --- brand -------------------------------------------------------------
    BRAND_NAME: str = "StelNexus"
    BRAND_TAGLINE: str = "Every fight, settled to the centavo"

    # The number game (Bola) is open to every role that holds its permissions (default). Set
    # BOLA_DEV_ONLY=true to pull it back behind the hidden developer (Admin) account: every other
    # role, Pawn grants, the public live board and the Bola variables then go dark again.
    BOLA_DEV_ONLY: bool = False

    RATE_LIMIT_ENABLED: bool = True

    # --- several web workers (see docs/SCALING.md) --------------------------
    # Redis keeps what must be shared between workers: rate-limit counters, instant chat wake-ups, cache
    # invalidation, typing dots. Empty = single-process behaviour (fine for --workers 1). If Redis is configured
    # but down, every worker falls back to its own memory for a few seconds at a time instead of failing.
    REDIS_URL: str = ""
    REDIS_PREFIX: str = "stelnexus"
    REDIS_TIMEOUT: float = Field(default=0.25, gt=0, le=5)
    # Believe X-Forwarded-For hops that are Cloudflare addresses (set when the site is proxied by Cloudflare AND
    # Caddy trusts Cloudflare, see deploy/cloudflare.caddy). Without it every visitor looks like one Cloudflare IP.
    TRUST_CLOUDFLARE: bool = False
    # DATABASE_URL points at PgBouncer in transaction mode: turn off server-side prepared statements.
    DB_PGBOUNCER: bool = False
    SEED_DEMO: bool = False

    @field_validator("SECRET_KEY")
    @classmethod
    def _key_len(cls, v: str) -> str:
        if not v:
            return INSECURE_DEFAULT  # blank in .env -> treated as unset
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("UPLOAD_DIR", mode="before")
    @classmethod
    def _expand(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @model_validator(mode="after")
    def _prod_guards(self) -> "Settings":
        if self.ENV == "prod":
            if self.is_sqlite:
                raise ValueError(
                    "Refusing to start: DATABASE_URL is SQLite in prod. SQLite allows one writer at a "
                    "time, which bottlenecks bet placement and settlement. Use PostgreSQL "
                    "(postgresql+psycopg://...) and move existing data with "
                    "`python -m app.cli sqlite-to-postgres`.")
            if self.SECRET_KEY == INSECURE_DEFAULT:
                raise ValueError("Refusing to start: SECRET_KEY is the dev default in prod")
            if self.DEBUG:
                raise ValueError("Refusing to start: DEBUG=true in prod")
            if not self.COOKIE_SECURE:
                raise ValueError("Refusing to start: COOKIE_SECURE=false in prod")
            if "*" in self.TRUSTED_HOSTS:
                raise ValueError("Refusing to start: TRUSTED_HOSTS must be explicit in prod")
            if self.COOKIE_SAMESITE == "none":
                raise ValueError("Refusing to start: COOKIE_SAMESITE must be lax or strict in prod")
            if not self.PII_KEY:
                raise ValueError("Refusing to start: PII_KEY must be set in prod (python -m app.cli gen-keys)")
            if "e2b.app" in self.FRAME_ANCESTORS or "*" in self.FRAME_ANCESTORS:
                raise ValueError("Refusing to start: FRAME_ANCESTORS must be explicit in prod")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def session_idle_seconds(self) -> int:
        return self.SESSION_IDLE_MINUTES * 60

    @property
    def session_absolute_seconds(self) -> int:
        return self.SESSION_ABSOLUTE_HOURS * 3600


def _persist_env(name: str, value: str) -> bool:
    """Best-effort: write NAME=value into .env (chmod 600) so dev keys survive restarts."""
    import os
    path = BASE_DIR / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        out, done = [], False
        for line in lines:
            if line.strip().startswith(f"{name}="):
                out.append(line if line.split("=", 1)[1].strip() else f"{name}={value}")
                done = True
            else:
                out.append(line)
        if not done:
            out.append(f"{name}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.ENV == "dev":  # dev never runs on the public default key / without PII encryption
        import base64
        if s.SECRET_KEY == INSECURE_DEFAULT:
            s.SECRET_KEY = generate_secret()
            _persist_env("SECRET_KEY", s.SECRET_KEY)
        if not s.PII_KEY:
            s.PII_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
            _persist_env("PII_KEY", s.PII_KEY)
    return s


def generate_secret() -> str:
    """Helper for ``python -m app.core.config`` -> fresh SECRET_KEY."""
    return secrets.token_urlsafe(48)


if __name__ == "__main__":  # pragma: no cover
    print(generate_secret())
