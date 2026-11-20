"""Configuration loading.

Three YAML files drive behavior so products, timing and copy can change without code changes:
  config/settings.yaml   program settings, Apollo ids, caps, mode
  config/products.yaml   product classification table
  config/lifecycle.yaml  segments, touch timing, suppression rules
  content/emails.yaml    email copy (Jinja2 templates)

Secrets come only from environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

def _find_root() -> Path:
    """Project root holding config/ and content/: $HP_ROOT, else the working directory, else the source tree."""
    if os.environ.get("HP_ROOT"):
        return Path(os.environ["HP_ROOT"])
    if (Path.cwd() / "config" / "settings.yaml").exists():
        return Path.cwd()
    return Path(__file__).resolve().parents[2]


ROOT = _find_root()

VALID_MODES = ("dry_run", "sandbox", "live")


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _to_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


@dataclass
class Secrets:
    database_url: str
    qbo_client_id: str | None
    qbo_client_secret: str | None
    qbo_realm_id: str | None
    qbo_bootstrap_refresh_token: str | None
    apollo_api_key: str | None
    allow_live_send: bool
    alert_webhook_url: str | None
    token_encryption_key: str | None
    ms_tenant_id: str | None = None          # Microsoft Graph, read-only inbox search for the contact lookup
    ms_client_id: str | None = None
    ms_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            database_url=os.environ.get("DATABASE_URL", f"sqlite:///{ROOT / 'hunter_retention.db'}"),
            qbo_client_id=os.environ.get("QBO_CLIENT_ID"),
            qbo_client_secret=os.environ.get("QBO_CLIENT_SECRET"),
            qbo_realm_id=os.environ.get("QBO_REALM_ID"),
            qbo_bootstrap_refresh_token=os.environ.get("QBO_REFRESH_TOKEN"),
            apollo_api_key=os.environ.get("APOLLO_API_KEY"),
            allow_live_send=os.environ.get("HP_ALLOW_LIVE_SEND", "").lower() == "true",
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL"),
            token_encryption_key=os.environ.get("HP_TOKEN_KEY"),
            ms_tenant_id=os.environ.get("MS_TENANT_ID") or None,
            ms_client_id=os.environ.get("MS_CLIENT_ID") or None,
            ms_client_secret=os.environ.get("MS_CLIENT_SECRET") or None,
        )


@dataclass
class Config:
    settings: dict
    products: dict
    lifecycle: dict
    emails: dict
    secrets: Secrets = field(default_factory=Secrets.from_env)

    # convenience accessors -------------------------------------------------
    @property
    def mode(self) -> str:
        m = os.environ.get("HP_MODE") or self.settings.get("mode", "dry_run")
        if m not in VALID_MODES:
            raise ValueError(f"Invalid mode {m!r}; expected one of {VALID_MODES}")
        return m

    @property
    def timezone(self) -> str:
        return self.settings.get("timezone", "America/New_York")

    @property
    def program_start_date(self) -> date:
        d = _to_date(self.settings.get("program_start_date"))
        if d is None:
            raise ValueError("settings.program_start_date is required")
        return d

    @property
    def history_start_date(self) -> date:
        return _to_date(self.settings.get("history_start_date")) or date(2000, 1, 1)

    def s(self, dotted: str, default: Any = None) -> Any:
        """Read a dotted path from settings.yaml."""
        cur: Any = self.settings
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def lc(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.lifecycle
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def load_config(
    config_dir: str | Path | None = None,
    content_dir: str | Path | None = None,
    overrides: dict | None = None,
    secrets: Secrets | None = None,
) -> Config:
    cdir = Path(config_dir) if config_dir else ROOT / "config"
    tdir = Path(content_dir) if content_dir else ROOT / "content"
    settings = _load_yaml(cdir / "settings.yaml")
    products = _load_yaml(cdir / "products.yaml")
    lifecycle = _load_yaml(cdir / "lifecycle.yaml")
    emails = _load_yaml(tdir / "emails.yaml")
    if overrides:
        _deep_merge(settings, overrides.get("settings", {}))
        _deep_merge(products, overrides.get("products", {}))
        _deep_merge(lifecycle, overrides.get("lifecycle", {}))
        _deep_merge(emails, overrides.get("emails", {}))
    return Config(settings=settings, products=products, lifecycle=lifecycle, emails=emails,
                  secrets=secrets or Secrets.from_env())


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
