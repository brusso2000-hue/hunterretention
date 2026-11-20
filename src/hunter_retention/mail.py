"""Read-only search of Hunter's own Outlook mailboxes through Microsoft Graph.

Used only by the contact lookup: "who at this customer has emailed us?". It needs an Entra (Azure AD)
app registration with the application permission Mail.Read, limited to the mailboxes in
settings.contacts.mailboxes. Nothing is ever sent, moved or changed, and message text is not stored.

Secrets: MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET. Without them the lookup simply skips the inbox step.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import requests

from .contacts import MailMessage
from .http import RateLimiter, request_with_retry
from .util import parse_date

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
FIELDS = "from,toRecipients,ccRecipients,subject,body,receivedDateTime"


class GraphMail:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailboxes: list[str],
                 session: requests.Session | None = None, max_per_search: int = 50):
        if not (tenant_id and client_id and client_secret and mailboxes):
            raise ValueError("MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET and contacts.mailboxes are required")
        self.tenant, self.client_id, self.secret = tenant_id, client_id, client_secret
        self.mailboxes, self.max = mailboxes, max_per_search
        self.session = session or requests.Session()
        self.limiter = RateLimiter(per_second=4)
        self._token: str | None = None
        self._expires = 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._expires - 60:
            resp = request_with_retry(
                self.session, "POST", f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"})
            body = resp.json()
            self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}", "Prefer": 'outlook.body-content-type="text"',
                "ConsistencyLevel": "eventual"}

    def _search(self, kql: str, since: date) -> list[MailMessage]:
        out: list[MailMessage] = []
        for box in self.mailboxes:
            # /messages covers every folder, so Sent Items (our replies) are included
            resp = request_with_retry(self.session, "GET", f"{GRAPH}/users/{box}/messages", limiter=self.limiter,
                                      headers=self._auth(),
                                      params={"$search": f'"{kql}"', "$top": self.max, "$select": FIELDS})
            for m in resp.json().get("value", []):
                d = parse_date(m.get("receivedDateTime"))
                if d and d < since:
                    continue
                frm = (m.get("from") or {}).get("emailAddress") or {}
                if not frm.get("address"):
                    continue
                rcpts = [((r.get("emailAddress") or {}).get("name"), (r.get("emailAddress") or {}).get("address") or "")
                         for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
                out.append(MailMessage(frm.get("name"), frm["address"].lower(), rcpts, m.get("subject") or "",
                                       ((m.get("body") or {}).get("content") or "")[:6000], d))
        return out

    def by_domain(self, domain: str, since: date) -> list[MailMessage]:
        return self._search(f"participants:{domain}", since)

    def by_text(self, text: str, since: date) -> list[MailMessage]:
        return self._search(text.replace('"', " "), since)

    def check(self) -> list[dict]:
        """Diagnostics: can we read each mailbox, and does search work? Returns status and error codes only,
        never message content."""
        out = []
        auth = self._auth()
        plain = {"Authorization": auth["Authorization"]}
        tests = [("list one message", plain, {"$top": 1, "$select": "receivedDateTime"}),
                 ("search, plain headers", plain, {"$search": '"participants:hunterproducts.com"', "$top": 1,
                                                   "$select": "receivedDateTime"}),
                 ("search, as the lookup sends it", auth, {"$search": '"participants:hunterproducts.com"',
                                                           "$top": self.max, "$select": FIELDS})]
        for box in self.mailboxes:
            for name, headers, params in tests:
                r = self.session.get(f"{GRAPH}/users/{box}/messages", headers=headers, params=params, timeout=60)
                err = {}
                if r.status_code >= 400:
                    try:
                        err = (r.json() or {}).get("error") or {}
                    except ValueError:
                        err = {"message": r.text[:200]}
                out.append({"mailbox": box, "test": name, "status": r.status_code, "code": err.get("code"),
                            "message": (err.get("message") or "")[:240],
                            "found": len(r.json().get("value", [])) if r.status_code < 400 else None})
        return out
