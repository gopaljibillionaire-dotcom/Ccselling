"""config.py - Centralised configuration from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Set

from dotenv import load_dotenv

load_dotenv()


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


def _id_set(name: str) -> Set[int]:
    out: Set[int] = set()
    for part in _csv(name):
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development").lower())
    app_secret: str = field(default_factory=lambda: _env("APP_SECRET", "dev-secret-change-me-32chars-min!!"))
    public_app_url: str = field(default_factory=lambda: _env("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    brand_name: str = field(default_factory=lambda: _env("BRAND_NAME", "CCS Shop"))
    default_currency: str = field(default_factory=lambda: _env("DEFAULT_CURRENCY", "USD").upper())

    mongodb_uri: str = field(default_factory=lambda: _env("MONGODB_URI"))
    mongodb_db: str = field(default_factory=lambda: _env("MONGODB_DB", "ccs_shop"))
    mongodb_transactions: bool = field(default_factory=lambda: _bool("MONGODB_TRANSACTIONS", True))

    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_admin_ids: Set[int] = field(default_factory=lambda: _id_set("TELEGRAM_ADMIN_IDS"))
    telegram_webapp_url: str = field(default_factory=lambda: _env("TELEGRAM_WEBAPP_URL", "").rstrip("/"))

    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))

    session_ttl_hours: int = field(default_factory=lambda: _int("SESSION_TTL_HOURS", 24 * 7))
    session_idle_minutes: int = field(default_factory=lambda: _int("SESSION_IDLE_MINUTES", 60 * 24))
    admin_session_ttl_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_TTL_MINUTES", 60))
    admin_session_idle_minutes: int = field(default_factory=lambda: _int("ADMIN_SESSION_IDLE_MINUTES", 15))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    cookie_samesite: str = field(default_factory=lambda: _env("COOKIE_SAMESITE", "lax").lower())
    cookie_name: str = field(default_factory=lambda: _env("SESSION_COOKIE_NAME", "sid"))

    allowed_origins: List[str] = field(default_factory=lambda: _csv("ALLOWED_ORIGINS"))
    admin_allow_file_origin: bool = field(default_factory=lambda: _bool("ADMIN_ALLOW_FILE_ORIGIN", False))
    trust_proxy_hops: int = field(default_factory=lambda: _int("TRUST_PROXY_HOPS", 1))

    admin_require_mfa: bool = field(default_factory=lambda: _bool("ADMIN_REQUIRE_MFA", False))
    invite_only: bool = field(default_factory=lambda: _bool("INVITE_ONLY", False))

    redis_url: str = field(default_factory=lambda: _env("REDIS_URL"))
    rl_login_max: int = field(default_factory=lambda: _int("RL_LOGIN_MAX", 20))
    rl_login_window: int = field(default_factory=lambda: _int("RL_LOGIN_WINDOW_SECONDS", 900))
    rl_api_max: int = field(default_factory=lambda: _int("RL_API_MAX", 400))
    rl_api_window: int = field(default_factory=lambda: _int("RL_API_WINDOW_SECONDS", 900))
    rl_sensitive_max: int = field(default_factory=lambda: _int("RL_SENSITIVE_MAX", 30))
    rl_sensitive_window: int = field(default_factory=lambda: _int("RL_SENSITIVE_WINDOW_SECONDS", 900))

    max_concurrent_requests: int = field(default_factory=lambda: _int("MAX_CONCURRENT_REQUESTS", 40))
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", 1_000_000))

    plisio_api_key: str = field(default_factory=lambda: _env("PLISIO_API_KEY"))
    plisio_base_url: str = field(default_factory=lambda: _env("PLISIO_BASE_URL", "https://api.plisio.net/api/v1").rstrip("/"))
    plisio_sandbox: bool = field(default_factory=lambda: _bool("PLISIO_SANDBOX", True))
    checkout_reservation_minutes: int = field(default_factory=lambda: _int("CHECKOUT_RESERVATION_MINUTES", 65))

    wallet_deposit_min_minor: int = field(default_factory=lambda: _int("WALLET_DEPOSIT_MIN_MINOR", 3900))
    wallet_deposit_max_minor: int = field(default_factory=lambda: _int("WALLET_DEPOSIT_MAX_MINOR", 500000))

    imgbb_api_key: str = field(default_factory=lambda: _env("IMGBB_API_KEY"))
    image_host_allowlist: List[str] = field(
        default_factory=lambda: _csv("IMAGE_HOST_ALLOWLIST", "i.ibb.co,ibb.co,image.ibb.co")
    )
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def port(self) -> int:
        return _int("PORT", 8000)

    def effective_origins(self) -> List[str]:
        origins = set(self.allowed_origins)
        if self.public_app_url:
            origins.add(self.public_app_url)
        if self.telegram_webapp_url:
            origins.add(self.telegram_webapp_url)
        if self.admin_allow_file_origin:
            origins.add("null")
        return sorted(origins)

    def is_telegram_admin(self, tg_user_id: int | None) -> bool:
        if tg_user_id is None:
            return False
        return int(tg_user_id) in self.telegram_admin_ids

    def validate(self) -> None:
        problems: List[str] = []
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            problems.append("COOKIE_SAMESITE must be lax, strict or none")
        if self.is_production:
            if len(self.app_secret) < 32:
                problems.append("APP_SECRET must be at least 32 characters")
            if not self.mongodb_uri:
                problems.append("MONGODB_URI is required")
            if not self.telegram_bot_token:
                problems.append("TELEGRAM_BOT_TOKEN is required")
            if not self.public_app_url.startswith("https://"):
                problems.append("PUBLIC_APP_URL must be https:// in production")
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
