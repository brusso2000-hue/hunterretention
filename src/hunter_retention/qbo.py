"""QuickBooks Online client: OAuth refresh with token rotation, paginated query, Change Data Capture."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterator, Protocol

import requests
from sqlalchemy import insert, select, update

from .db import DB, oauth_tokens, utcnow
from .http import ApiError, RateLimiter, request_with_retry

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
BASE = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}
SALES_ENTITIES = ["Invoice", "SalesReceipt", "CreditMemo", "RefundReceipt"]
CDC_ENTITIES = ["Customer", "Item", *SALES_ENTITIES]


class QBOSource(Protocol):
    """Interface the sync layer depends on (real client or test fake)."""

    def query_all(self, entity: str, where: str | None = None) -> Iterator[dict]: ...
    def cdc(self, entities: list[str], changed_since: datetime) -> dict[str, list[dict]]: ...


class TokenStore:
    """Persists the rotating refresh token. Optional Fernet encryption at rest (HP_TOKEN_KEY)."""

    def __init__(self, db: DB, encryption_key: str | None = None):
        self.db = db
        self._fernet = None
        if encryption_key:
            from cryptography.fernet import Fernet  # optional dependency
            self._fernet = Fernet(encryption_key.encode())

    def _enc(self, v: str | None) -> str | None:
        if v is None or not self._fernet:
            return v
        return "enc:" + self._fernet.encrypt(v.encode()).decode()

    def _dec(self, v: str | None) -> str | None:
        if v is None or not v.startswith("enc:"):
            return v
        if not self._fernet:
            raise RuntimeError("Stored token is encrypted but HP_TOKEN_KEY is not set")
        return self._fernet.decrypt(v[4:].encode()).decode()

    def load(self) -> dict | None:
        with self.db.engine.begin() as c:
            row = c.execute(select(oauth_tokens).where(oauth_tokens.c.provider == "qbo")).mappings().first()
        if not row:
            return None
        d = dict(row)
        d["refresh_token"] = self._dec(d["refresh_token"])
        d["access_token"] = self._dec(d["access_token"])
        return d

    def save(self, refresh_token: str, access_token: str | None = None,
             access_expires_at: datetime | None = None, refresh_expires_at: datetime | None = None) -> None:
        vals = dict(refresh_token=self._enc(refresh_token), access_token=self._enc(access_token),
                    access_expires_at=access_expires_at, refresh_expires_at=refresh_expires_at,
                    updated_at=utcnow())
        with self.db.engine.begin() as c:
            if c.execute(select(oauth_tokens.c.provider).where(oauth_tokens.c.provider == "qbo")).first():
                c.execute(update(oauth_tokens).where(oauth_tokens.c.provider == "qbo").values(**vals))
            else:
                c.execute(insert(oauth_tokens).values(provider="qbo", **vals))


class QBOClient:
    def __init__(self, client_id: str, client_secret: str, realm_id: str, tokens: TokenStore,
                 environment: str = "production", minor_version: int = 75,
                 bootstrap_refresh_token: str | None = None, session: requests.Session | None = None):
        if not (client_id and client_secret and realm_id):
            raise ValueError("QBO_CLIENT_ID, QBO_CLIENT_SECRET and QBO_REALM_ID are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.realm_id = realm_id
        self.tokens = tokens
        self.base = f"{BASE[environment]}/v3/company/{realm_id}"
        self.minor_version = minor_version
        self.session = session or requests.Session()
        self.limiter = RateLimiter(per_second=6)   # well under 500/min and 10 concurrent
        self._access: str | None = None
        self._access_exp: datetime | None = None
        stored = tokens.load()
        if not stored and bootstrap_refresh_token:
            tokens.save(bootstrap_refresh_token)

    # ---------------------------------------------------------------- auth
    def _refresh(self) -> None:
        stored = self.tokens.load()
        if not stored or not stored.get("refresh_token"):
            raise RuntimeError("No QBO refresh token stored. Run `hunter-retention set-qbo-token <token>`.")
        resp = request_with_retry(
            self.session, "POST", TOKEN_URL, max_attempts=3,
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json"},
            data={"grant_type": "refresh_token", "refresh_token": stored["refresh_token"]},
        )
        body = resp.json()
        now = utcnow()
        self._access = body["access_token"]
        self._access_exp = now + timedelta(seconds=int(body.get("expires_in", 3600)) - 300)
        hard = body.get("x_refresh_token_expires_in")
        refresh_exp = now + timedelta(seconds=int(hard)) if hard else None
        # Intuit rotates refresh tokens: ALWAYS persist the newest one before doing anything else.
        self.tokens.save(body["refresh_token"], self._access, self._access_exp, refresh_exp)
        if refresh_exp and refresh_exp - now < timedelta(days=30):
            log.warning("QBO refresh token expires %s; re-authorize soon", refresh_exp.isoformat())

    def _token(self) -> str:
        if not self._access or not self._access_exp or utcnow() >= self._access_exp:
            self._refresh()
        return self._access  # type: ignore[return-value]

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "minorversion": self.minor_version}
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
            try:
                resp = request_with_retry(self.session, "GET", f"{self.base}/{path}", params=params,
                                          headers=headers, limiter=self.limiter)
                return resp.json()
            except ApiError as exc:
                if exc.status == 401 and attempt == 1:
                    self._access = None          # force refresh once
                    continue
                raise
        raise ApiError("unreachable")

    # ---------------------------------------------------------------- data
    def query_all(self, entity: str, where: str | None = None) -> Iterator[dict]:
        start, page = 1, 1000
        while True:
            q = f"SELECT * FROM {entity}"
            if where:
                q += f" WHERE {where}"
            q += f" ORDERBY Id STARTPOSITION {start} MAXRESULTS {page}"
            body = self._get("query", {"query": q})
            rows = body.get("QueryResponse", {}).get(entity, []) or []
            yield from rows
            if len(rows) < page:
                return
            start += page

    def cdc(self, entities: list[str], changed_since: datetime) -> dict[str, list[dict]]:
        body = self._get("cdc", {"entities": ",".join(entities),
                                 "changedSince": changed_since.strftime("%Y-%m-%dT%H:%M:%S-00:00")})
        out: dict[str, list[dict]] = {e: [] for e in entities}
        for resp in body.get("CDCResponse", []) or []:
            for qr in resp.get("QueryResponse", []) or []:
                for ent in entities:
                    out[ent].extend(qr.get(ent, []) or [])
        return out
