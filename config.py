"""config.py - Centralised configuration, read exclusively from environment variables.

Nothing secret is ever hard-coded. In production (APP_ENV=production) missing or weak
critical settings abort start-up (fail closed) with a clear diagnostic message.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _csv(name: str, default: str = "") -> List[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # --- Application ---------------------------------------------------------
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development").lower())
    app_secret: str = field(default_factory=lambda: _env("APP_SECRET"))
    public_app_url: str = field(default_factory=lambda: _env("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    brand_name: str = field(default_factory=lambda: _env("BRAND_NAME", "CCs shop"))
    default_currency: str = field(default_factory=lambda: _env("DEFAULT_CURRENCY", "USD").upper())

    # --- MongoDB -------------------------------------------------------------
    mongodb_uri: str = field(default_factory=lambda: _env("MONGODB_URI"))
    mongodb_db: str = field(default_factory=lambda: _env("MONGODB_DB", "marketplace"))
    mongodb_transactions: bool = field(default_factory=lambda: _bool("MONGODB_TRANSACTIONS", True))

    # --- Sessions / cookies --------------------------------------------------
    session_ttl_hours: int = field(default_factory=lambda: _int("SESSION_TTL_HOURS", 24 * 7))
    session_idle_minutes: int = field(default_factory=lambda: _int("SESSION_IDLE_MINUTES", 60 * 24))
    admin_session_ttl_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_TTL_MINUTES", 60))
    admin_session_idle_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_IDLE_MINUTES", 15))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    cookie_samesite: str = field(default_factory=lambda: _env("COOKIE_SAMESITE", "lax").lower())
    cookie_name: str = field(default_factory=lambda: _env("SESSION_COOKIE_NAME", "sid"))

    # --- Origins / proxies ---------------------------------------------------
    allowed_origins: List[str] = field(default_factory=lambda: _csv("ALLOWED_ORIGINS"))
    admin_allow_file_origin: bool = field(default_factory=lambda: _bool("ADMIN_ALLOW_FILE_ORIGIN", False))
    trust_proxy_hops: int = field(default_factory=lambda: _int("TRUST_PROXY_HOPS", 1))

    # --- Admin policy --------------------------------------------------------
    admin_require_mfa: bool = field(default_factory=lambda: _bool("ADMIN_REQUIRE_MFA", True))
    invite_only: bool = field(default_factory=lambda: _bool("INVITE_ONLY", False))

    # --- Rate limiting -------------------------------------------------------
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL"))
    rl_login_max: int = field(default_factory=lambda: _int("RL_LOGIN_MAX", 10))
    rl_login_window: int = field(default_factory=lambda: _int("RL_LOGIN_WINDOW_SECONDS", 900))
    rl_api_max: int = field(default_factory=lambda: _int("RL_API_MAX", 100))
    rl_api_window: int = field(default_factory=lambda: _int("RL_API_WINDOW_SECONDS", 900))
    rl_sensitive_max: int = field(default_factory=lambda: _int("RL_SENSITIVE_MAX", 20))
    rl_sensitive_window: int = field(default_factory=lambda: _int("RL_SENSITIVE_WINDOW_SECONDS", 900))
    rl_payment_max: int = field(default_factory=lambda: _int("RL_PAYMENT_MAX", 5))
    rl_payment_window: int = field(default_factory=lambda: _int("RL_PAYMENT_WINDOW_SECONDS", 900))
    rl_reset_max: int = field(default_factory=lambda: _int("RL_RESET_MAX", 5))
    rl_reset_window: int = field(default_factory=lambda: _int("RL_RESET_WINDOW_SECONDS", 3600))
    search_max_query_length: int = field(default_factory=lambda: _int("SEARCH_MAX_QUERY_LENGTH", 80))

    # --- Plisio (https://plisio.net/documentation) ---------------------------
    plisio_api_key: str = field(default_factory=lambda: _env("PLISIO_API_KEY"))
    plisio_base_url: str = field(default_factory=lambda: _env("PLISIO_BASE_URL", "https://api.plisio.net/api/v1").rstrip("/"))
    plisio_sandbox: bool = field(default_factory=lambda: _bool("PLISIO_SANDBOX", True))
    checkout_reservation_minutes: int = field(default_factory=lambda: _int("CHECKOUT_RESERVATION_MINUTES", 65))

    # --- ImgBB ---------------------------------------------------------------
    imgbb_api_key: str = field(default_factory=lambda: _env("IMGBB_API_KEY"))
    image_host_allowlist: List[str] = field(
        default_factory=lambda: _csv("IMAGE_HOST_ALLOWLIST", "i.ibb.co,ibb.co,image.ibb.co,i.ibb.co.com")
    )
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def plisio_enabled(self) -> bool:
        return bool(self.plisio_api_key)

    @property
    def imgbb_enabled(self) -> bool:
        return bool(self.imgbb_api_key)

    def effective_origins(self) -> List[str]:
        origins = set(self.allowed_origins)
        if self.public_app_url:
            origins.add(self.public_app_url)
        if self.admin_allow_file_origin:
            origins.add("null")  # origin sent by pages opened from file://
        return sorted(origins)

    def validate(self) -> None:
        """Fail safely: refuse to start in production with missing/weak critical settings."""
        problems: List[str] = []
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            problems.append("COOKIE_SAMESITE must be lax, strict or none")
        if self.is_production:
            if len(self.app_secret) < 32:
                problems.append("APP_SECRET must be set to a random string of at least 32 characters")
            if not self.mongodb_uri:
                problems.append("MONGODB_URI is required")
            if not self.public_app_url.startswith("https://"):
                problems.append("PUBLIC_APP_URL must be an https:// URL in production")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true in production")
            if not self.admin_require_mfa:
                problems.append("ADMIN_REQUIRE_MFA must stay enabled in production")
            if self.plisio_enabled and self.plisio_sandbox:
                logging.getLogger("config").warning("Plisio is in SANDBOX mode in production; no real payments will clear")
        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))


def load_settings() -> Settings:
    s = Settings()
    s.validate()
    return s


settings = load_settings() if os.environ.get("SKIP_CONFIG_VALIDATION") != "1" else Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
