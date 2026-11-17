"""Apollo.io client (master API key). Only the endpoints this system needs.

Verified against docs.apollo.io (Nov 2026):
  POST /contacts/search                               find contact by email
  POST /contacts                (run_dedupe=true)     create
  PUT  /contacts/{id}                                 update incl. typed_custom_fields
  GET  /fields?source=custom  (fallback /typed_custom_fields)   list custom fields
  POST /fields                                        create custom field
  POST /emailer_campaigns/{id}/add_contact_ids        enroll (0 credits, 600/hr)
  POST /emailer_campaigns/remove_or_stop_contact_ids  stop / finish / remove (0 credits, 600/hr)
  POST /emailer_messages/search                       engagement (best effort)
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import requests

from .http import ApiError, RateLimiter, request_with_retry

log = logging.getLogger(__name__)

BASE = "https://api.apollo.io/api/v1"


class SkippedByApollo(Exception):
    """Apollo accepted the call but did not enroll the contact (unsubscribed, active elsewhere, ...)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ApolloAPI(Protocol):
    def find_contact(self, email: str) -> dict | None: ...
    def create_contact(self, payload: dict) -> dict: ...
    def update_contact(self, contact_id: str, payload: dict) -> dict: ...
    def list_custom_fields(self) -> list[dict]: ...
    def create_custom_field(self, label: str, field_type: str) -> dict: ...
    def add_to_sequence(self, sequence_id: str, contact_id: str, email_account_id: str) -> dict: ...
    def remove_from_sequence(self, sequence_ids: list[str], contact_id: str, mode: str = "remove") -> dict: ...
    def message_activity(self, contact_id: str, sequence_ids: list[str]) -> list[dict]: ...


class ApolloClient:
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise ValueError("APOLLO_API_KEY (master key) is required")
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Content-Type": "application/json",
                                     "Accept": "application/json", "Cache-Control": "no-cache"})
        self.limiter = RateLimiter(per_second=2)

    def _call(self, method: str, path: str, **kw) -> dict:
        resp = request_with_retry(self.session, method, f"{BASE}{path}", limiter=self.limiter, **kw)
        return resp.json() if resp.content else {}

    # ---------------------------------------------------------------- contacts
    def find_contact(self, email: str) -> dict | None:
        body = self._call("POST", "/contacts/search", json={"q_keywords": email, "per_page": 10})
        for c in body.get("contacts", []) or []:
            if (c.get("email") or "").lower() == email.lower():
                return c
        return None

    def create_contact(self, payload: dict) -> dict:
        body = self._call("POST", "/contacts", json={**payload, "run_dedupe": True})
        return body.get("contact") or body

    def update_contact(self, contact_id: str, payload: dict) -> dict:
        body = self._call("PUT", f"/contacts/{contact_id}", json=payload)
        return body.get("contact") or body

    # ---------------------------------------------------------------- fields
    def list_custom_fields(self) -> list[dict]:
        try:
            body = self._call("GET", "/fields", params={"source": "custom", "modality": "contact"})
            return body.get("fields") or body.get("typed_custom_fields") or []
        except ApiError as exc:
            if exc.status not in (404, 422):
                raise
            body = self._call("GET", "/typed_custom_fields")
            return body.get("typed_custom_fields") or []

    def create_custom_field(self, label: str, field_type: str) -> dict:
        body = self._call("POST", "/fields", json={"label": label, "modality": "contact", "type": field_type})
        created = body.get("typed_custom_fields") or body.get("fields") or [body.get("field") or body]
        return created[0] if isinstance(created, list) else created

    # ---------------------------------------------------------------- sequences
    def add_to_sequence(self, sequence_id: str, contact_id: str, email_account_id: str) -> dict:
        body = self._call("POST", f"/emailer_campaigns/{sequence_id}/add_contact_ids", json={
            "emailer_campaign_id": sequence_id,
            "contact_ids": [contact_id],
            "send_email_from_email_account_id": email_account_id,
            "sequence_no_email": False,
            "sequence_unverified_email": True,        # QBO billing addresses are real but often unverified in Apollo
            "sequence_job_change": False,
            "sequence_active_in_other_campaigns": False,   # never stack on top of a live cold/reactivation sequence
            "sequence_finished_in_other_campaigns": True,  # must be true: they finished our previous touch
        })
        skipped = _skipped_reason(body, contact_id)
        if skipped:
            raise SkippedByApollo(skipped)
        return body

    def remove_from_sequence(self, sequence_ids: list[str], contact_id: str, mode: str = "remove") -> dict:
        return self._call("POST", "/emailer_campaigns/remove_or_stop_contact_ids", json={
            "emailer_campaign_ids": sequence_ids, "contact_ids": [contact_id], "mode": mode})

    def message_activity(self, contact_id: str, sequence_ids: list[str]) -> list[dict]:
        body = self._call("POST", "/emailer_messages/search", json={
            "contact_ids": [contact_id], "emailer_campaign_ids": sequence_ids, "per_page": 50})
        return body.get("emailer_messages") or []


def _skipped_reason(body: dict, contact_id: str) -> str | None:
    """Apollo reports contacts it refused under several keys depending on version; check them all."""
    for key in ("skipped_contact_ids", "contacts_skipped", "not_added_contact_ids"):
        val = body.get(key)
        if isinstance(val, dict):
            for reason, ids in val.items():
                if contact_id in (ids or []):
                    return str(reason)
        elif isinstance(val, list) and contact_id in val:
            return key
    added = body.get("contacts")
    if isinstance(added, list) and added and not any(c.get("id") == contact_id for c in added):
        return "not_in_added_contacts"
    return None


def contact_flags(contact: dict | None) -> dict[str, Any]:
    """Normalize unsubscribe / bounce / active-elsewhere state from a contact record."""
    if not contact:
        return {"unsubscribed": False, "bounced": False, "active_sequences": [], "statuses": []}
    statuses = contact.get("contact_campaign_statuses") or []
    active = [s.get("emailer_campaign_id") for s in statuses if (s.get("status") or "").lower() in ("active", "paused")]
    email_status = (contact.get("email_status") or "").lower()
    return {
        "unsubscribed": bool(contact.get("email_unsubscribed")),
        "bounced": email_status in ("bounced", "invalid", "hard_bounced"),
        "active_sequences": [a for a in active if a],
        "statuses": statuses,
    }


def message_flags(messages: list[dict]) -> dict[str, bool]:
    """Tolerant parse of emailer_messages. Field names verified on first sandbox run (see docs/RUNBOOK.md)."""
    out = {"delivered": False, "opened": False, "clicked": False, "replied": False, "bounced": False,
           "unsubscribed": False}
    for m in messages:
        status = str(m.get("status") or "").lower()
        if status in ("completed", "delivered", "sent") or m.get("completed_at"):
            out["delivered"] = True
        if m.get("num_opens") or m.get("opened_at") or m.get("open_tracked") and m.get("opened"):
            out["opened"] = True
        if m.get("num_clicks") or m.get("clicked_at"):
            out["clicked"] = True
        if m.get("replied") or m.get("replied_at") or "repl" in status:
            out["replied"] = True
        if m.get("bounce") or m.get("bounced") or "bounce" in status:
            out["bounced"] = True
        if m.get("unsubscribed") or m.get("unsubscribed_at"):
            out["unsubscribed"] = True
    return out
