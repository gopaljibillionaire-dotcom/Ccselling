"""config.py - Every setting comes from environment variables (Heroku Config Vars) via os.getenv.

Set them with:  heroku config:set NAME=value   (or Dashboard -> Settings -> Reveal Config Vars)
Nothing secret is hard-coded here. In production (APP_ENV=production) the app refuses to start
when critical settings are missing or unsafe.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Config Var {name} must be an integer, got {raw!r}") from exc


def _csv(name: str, default: str = "") -> List[str]:
    return [p.strip() for p in _str(name, default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # ---- Application -------------------------------------------------------------
    app_env: str = field(default_factory=lambda: _str("APP_ENV", "development").lower())       # development | production
    app_secret: str = field(default_factory=lambda: _str("APP_SECRET"))                         # 32+ random chars (required in production)
    public_app_url: str = field(default_factory=lambda: _str("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())
    brand_name: str = field(default_factory=lambda: _str("BRAND_NAME", "Auralis"))
    default_currency: str = field(default_factory=lambda: _str("DEFAULT_CURRENCY", "USD").upper())
    port: int = field(default_factory=lambda: _int("PORT", 8000))                               # set automatically by Heroku

    # ---- MongoDB Atlas -----------------------------------------------------------
    mongodb_uri: str = field(default_factory=lambda: _str("MONGODB_URI"))
    mongodb_db: str = field(default_factory=lambda: _str("MONGODB_DB", "marketplace"))
    mongodb_transactions: bool = field(default_factory=lambda: _bool("MONGODB_TRANSACTIONS", True))

    # ---- Sessions / cookies ------------------------------------------------------
    session_ttl_hours: int = field(default_factory=lambda: _int("SESSION_TTL_HOURS", 24 * 7))
    session_idle_minutes: int = field(default_factory=lambda: _int("SESSION_IDLE_MINUTES", 60 * 24))
    admin_session_ttl_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_TTL_MINUTES", 60))
    admin_session_idle_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_IDLE_MINUTES", 15))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    cookie_samesite: str = field(default_factory=lambda: _str("COOKIE_SAMESITE", "lax").lower())
    cookie_name: str = field(default_factory=lambda: _str("SESSION_COOKIE_NAME", "sid"))

    # ---- Origins / proxies -------------------------------------------------------
    allowed_origins: List[str] = field(default_factory=lambda: _csv("ALLOWED_ORIGINS"))        # comma separated
    admin_allow_file_origin: bool = field(default_factory=lambda: _bool("ADMIN_ALLOW_FILE_ORIGIN", False))
    trust_proxy_hops: int = field(default_factory=lambda: _int("TRUST_PROXY_HOPS", 1))

    # ---- Policy ------------------------------------------------------------------
    admin_require_mfa: bool = field(default_factory=lambda: _bool("ADMIN_REQUIRE_MFA", True))
    invite_only: bool = field(default_factory=lambda: _bool("INVITE_ONLY", False))
    referral_reward_points: int = field(default_factory=lambda: _int("REFERRAL_REWARD_POINTS", 100))
    wallet_deposit_min_minor: int = field(default_factory=lambda: _int("WALLET_DEPOSIT_MIN_MINOR", 500))       # 500 = 5.00
    wallet_deposit_max_minor: int = field(default_factory=lambda: _int("WALLET_DEPOSIT_MAX_MINOR", 500000))   # 500000 = 5,000.00

    # ---- Rate limiting -----------------------------------------------------------
    redis_url: str = field(default_factory=lambda: _str("REDIS_URL"))                           # optional (Heroku Redis sets it)
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

    # ---- OxaPay (OxaPay signs webhooks with HMAC-SHA512 using the merchant key itself) -----
    oxapay_merchant_api_key: str = field(default_factory=lambda: _str("OXAPAY_MERCHANT_API_KEY"))
    oxapay_base_url: str = field(default_factory=lambda: _str("OXAPAY_BASE_URL", "https://api.oxapay.com/v1").rstrip("/"))
    oxapay_sandbox: bool = field(default_factory=lambda: _bool("OXAPAY_SANDBOX", True))
    oxapay_invoice_lifetime_min: int = field(default_factory=lambda: _int("OXAPAY_INVOICE_LIFETIME_MINUTES", 60))
    checkout_reservation_minutes: int = field(default_factory=lambda: _int("CHECKOUT_RESERVATION_MINUTES", 65))
    reconcile_interval_seconds: int = field(default_factory=lambda: _int("RECONCILE_INTERVAL_SECONDS", 60))

    # ---- ImgBB -------------------------------------------------------------------
    imgbb_api_key: str = field(default_factory=lambda: _str("IMGBB_API_KEY"))
    imgbb_upload_url: str = field(default_factory=lambda: _str("IMGBB_UPLOAD_URL", "https://api.imgbb.com/1/upload"))
    image_host_allowlist: List[str] = field(default_factory=lambda: _csv("IMAGE_HOST_ALLOWLIST", "i.ibb.co,ibb.co,image.ibb.co,i.ibb.co.com"))
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))

    # ---- Derived helpers ---------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def oxapay_enabled(self) -> bool:
        return bool(self.oxapay_merchant_api_key)

    @property
    def imgbb_enabled(self) -> bool:
        return bool(self.imgbb_api_key)

    def effective_origins(self) -> List[str]:
        origins = set(self.allowed_origins)
        if self.public_app_url:
            origins.add(self.public_app_url)
        if self.admin_allow_file_origin:
            origins.add("null")  # Origin header sent by pages opened from file://
        return sorted(origins)

    def validate(self) -> None:
        """Fail safely: refuse to start in production with missing or weak critical settings."""
        problems: List[str] = []
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            problems.append("COOKIE_SAMESITE must be lax, strict or none")
        if self.wallet_deposit_min_minor <= 0 or self.wallet_deposit_max_minor < self.wallet_deposit_min_minor:
            problems.append("WALLET_DEPOSIT_MIN_MINOR/MAX_MINOR are inconsistent")
        if self.is_production:
            if len(self.app_secret) < 32:
                problems.append("APP_SECRET must be a random string of at least 32 characters")
            if not self.mongodb_uri:
                problems.append("MONGODB_URI is required")
            if not self.public_app_url.startswith("https://"):
                problems.append("PUBLIC_APP_URL must be an https:// URL")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true")
            if not self.admin_require_mfa:
                problems.append("ADMIN_REQUIRE_MFA must stay enabled")
            if self.oxapay_enabled and self.oxapay_sandbox:
                logging.getLogger("config").warning("OxaPay is in SANDBOX mode in production; no real payments will clear")
        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))


def load_settings() -> Settings:
    s = Settings()
    s.validate()
    return s


settings = Settings() if os.getenv("SKIP_CONFIG_VALIDATION") == "1" else load_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
