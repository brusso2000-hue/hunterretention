"""Failure alerts. Posts to ALERT_WEBHOOK_URL (Teams/Slack-compatible {"text": ...}) if configured.
The scheduled job also exits non-zero on fatal errors, so GitHub Actions emails the repo owner."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, webhook_url: str | None, session: requests.Session | None = None):
        self.url = webhook_url
        self.session = session or requests.Session()
        self.sent: list[str] = []

    def alert(self, title: str, detail: str = "") -> None:
        msg = f"[Hunter retention] {title}" + (f"\n{detail}" if detail else "")
        self.sent.append(msg)
        log.error(msg)
        if not self.url:
            return
        try:
            self.session.post(self.url, json={"text": msg}, timeout=15)
        except Exception as exc:  # alerting must never crash the run
            log.error("alert webhook failed: %s", exc)
