"""Test harness: in-memory fakes for QuickBooks and Apollo, plus helpers to drive the daily job by date."""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest

from hunter_retention.alerts import Alerter
from hunter_retention.apollo import SkippedByApollo
from hunter_retention.config import Secrets, load_config
from hunter_retention.db import DB, apollo_fields, approvals, utcnow
from hunter_retention.dispatch import HP_FIELDS
from hunter_retention.runner import run_daily

SEQS = {"e1": "seq_e1", "e2": "seq_e2", "e3": "seq_e3", "e4": "seq_e4",
        "r1": "seq_r1", "r2": "seq_r2", "r3": "seq_r3"}


class FakeQBO:
    def __init__(self):
        self.customers: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.txns: dict[str, dict[str, dict]] = {e: {} for e in ("Invoice", "SalesReceipt", "CreditMemo", "RefundReceipt")}
        self.deleted: list[tuple[str, str, datetime]] = []
        self._ids = itertools.count(1000)
        self.fail = False
        self.calls: list[str] = []
        self.cdc_hidden: set[str] = set()     # simulate QBO CDC lag: visible only to a direct query

    # -- builders ----------------------------------------------------------
    def customer(self, cid, name, email=None, given=None, company=None, parent=None):
        self.customers[cid] = {"Id": cid, "DisplayName": name, "CompanyName": company or name, "GivenName": given,
                               "FamilyName": "Buyer" if given else None,
                               "PrimaryEmailAddr": {"Address": email} if email else None,
                               "ParentRef": {"value": parent} if parent else None, "Active": True,
                               "MetaData": {"LastUpdatedTime": "2026-01-01T00:00:00-08:00"}}
        return cid

    def order(self, cid, txn_date, lines, kind="Invoice", ship_date=None, country=None, note=None, tid=None,
              ship=None, po=None):
        tid = tid or str(next(self._ids))
        total = sum(q * p for _, q, p in lines)
        self.txns[kind][tid] = {
            "Id": tid, "CustomerRef": {"value": cid}, "TxnDate": str(txn_date),
            "ShipDate": str(ship_date) if ship_date else None, "TotalAmt": total, "PrivateNote": note,
            "ShipAddr": ({**({"Country": country} if country else {}),
                          **{f"Line{i + 1}": v for i, v in enumerate(ship or [])}} or None),
            "CustomField": [{"DefinitionId": "1", "Name": "P.O. Number", "Type": "StringType",
                             "StringValue": po}] if po else [],
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": q * p,
                      "SalesItemLineDetail": {"ItemRef": {"value": f"i-{sku}", "name": sku}, "Qty": q}}
                     for sku, q, p in lines],
            "MetaData": {"LastUpdatedTime": f"{txn_date}T12:00:00-08:00"},
        }
        return tid

    def void(self, tid, kind="Invoice"):
        t = self.txns[kind][tid]
        t["TotalAmt"] = 0
        t["PrivateNote"] = "Voided"
        for ln in t["Line"]:
            ln["Amount"] = 0

    def delete(self, tid, kind="Invoice"):
        self.txns[kind].pop(tid)
        self.deleted.append((kind, tid, datetime.now()))

    # -- QBOSource ---------------------------------------------------------
    def query_all(self, entity, where=None):
        self.calls.append(f"query:{entity}")
        if self.fail:
            raise RuntimeError("QuickBooks unavailable")
        if entity == "Customer":
            return list(self.customers.values())
        if entity == "Item":
            return list(self.items.values())
        rows = list(self.txns[entity].values())
        if where and "CustomerRef IN" in where:
            ids = where.split("(")[1].split(")")[0].replace("'", "").split(",")
            rows = [r for r in rows if r["CustomerRef"]["value"] in ids]
        return rows

    def cdc(self, entities, changed_since):
        self.calls.append("cdc")
        if self.fail:
            raise RuntimeError("QuickBooks unavailable")
        out = {e: [] for e in entities}
        out["Customer"] = list(self.customers.values())
        out["Item"] = list(self.items.values())
        for e in ("Invoice", "SalesReceipt", "CreditMemo", "RefundReceipt"):
            out[e] = [t for tid, t in self.txns[e].items() if tid not in self.cdc_hidden]
        for kind, tid, _ in self.deleted:
            out[kind].append({"Id": tid, "status": "Deleted"})
        return out


class FakeApollo:
    def __init__(self):
        self.contacts: dict[str, dict] = {}
        self.enrollments: list[tuple[str, str]] = []
        self.removals: list[tuple[list, str, str]] = []
        self.writes = 0
        self.fail_enroll_times = 0
        self.skip_reason: str | None = None
        self.messages: dict[str, list[dict]] = {}
        self._ids = itertools.count(1)

    def _by_email(self, email):
        for c in self.contacts.values():
            if c["email"] == email.lower():
                return c
        return None

    def find_contact(self, email):
        return self._by_email(email)

    def create_contact(self, payload):
        self.writes += 1
        existing = self._by_email(payload["email"])
        if existing:
            return existing
        cid = f"c{next(self._ids)}"
        c = {"id": cid, "email": payload["email"].lower(), "email_unsubscribed": False, "email_status": "verified",
             "contact_campaign_statuses": [], "typed_custom_fields": dict(payload.get("typed_custom_fields", {}))}
        self.contacts[cid] = c
        return c

    def update_contact(self, contact_id, payload):
        self.writes += 1
        self.contacts[contact_id]["typed_custom_fields"].update(payload.get("typed_custom_fields", {}))
        return self.contacts[contact_id]

    def list_custom_fields(self):
        return [{"id": f"f_{k}", "label": k} for k in HP_FIELDS]

    def create_custom_field(self, label, field_type):
        return {"id": f"f_{label}", "label": label}

    def add_to_sequence(self, sequence_id, contact_id, email_account_id):
        self.writes += 1
        if self.fail_enroll_times > 0:
            self.fail_enroll_times -= 1
            raise RuntimeError("Apollo 503")
        if self.skip_reason:
            raise SkippedByApollo(self.skip_reason)
        c = self.contacts[contact_id]
        if any(s["emailer_campaign_id"] == sequence_id for s in c["contact_campaign_statuses"]):
            raise RuntimeError("already in sequence")
        c["contact_campaign_statuses"].append({"emailer_campaign_id": sequence_id, "status": "finished"})
        self.enrollments.append((sequence_id, c["email"]))
        return {"contacts": [{"id": contact_id}]}

    def remove_from_sequence(self, sequence_ids, contact_id, mode="remove"):
        self.writes += 1
        self.removals.append((sequence_ids, contact_id, mode))
        c = self.contacts[contact_id]
        c["contact_campaign_statuses"] = [s for s in c["contact_campaign_statuses"]
                                          if s["emailer_campaign_id"] not in sequence_ids]
        return {}

    def message_activity(self, contact_id, sequence_ids):
        return self.messages.get(contact_id, [])

    # helpers
    def sent_to(self, email):
        return [s for s, e in self.enrollments if e == email.lower()]


class Harness:
    def __init__(self, tmp_path, mode="live", settings=None, lifecycle=None, approve=True, allow=True):
        base = {"mode": mode, "program_start_date": "2026-01-01", "history_start_date": "2015-01-01",
                "apollo": {"sequences": dict(SEQS), "sequence_pool": []}, "analytics": {"holdout_pct": 0}}
        ov = {"settings": base, "lifecycle": lifecycle or {}}
        if settings:
            from hunter_retention.config import _deep_merge
            _deep_merge(base, settings)
        secrets = Secrets(database_url=f"sqlite:///{tmp_path}/t.db", qbo_client_id=None, qbo_client_secret=None,
                          qbo_realm_id=None, qbo_bootstrap_refresh_token=None, apollo_api_key="x",
                          allow_live_send=allow, alert_webhook_url=None, token_encryption_key=None)
        self.cfg = load_config(overrides=ov, secrets=secrets)
        # tests use approved copy: strip VERIFY markers unless a test wants them
        self.db = DB(secrets.database_url)
        self.db.create_all()
        self.qbo = FakeQBO()
        self.apollo = FakeApollo()
        self.alerter = Alerter(None)
        self.tmp = tmp_path
        with self.db.engine.begin() as c:
            for k in HP_FIELDS:
                c.execute(apollo_fields.insert().values(label=k, field_id=f"f_{k}", type="string"))
            if approve:
                c.execute(approvals.insert().values(key="production_send", approved_by="test", approved_at=utcnow()))

    def strip_verify(self):
        import re
        def clean(o):
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items()}
            if isinstance(o, str):
                return re.sub(r"\[\[\s*VERIFY:[^\]]*\]\]", "approved text", o)
            return o
        self.cfg.emails = clean(self.cfg.emails)
        return self

    def run(self, day: date, **kw):
        import os
        os.environ.pop("HP_MODE", None)
        return run_daily(self.cfg, self.db, self.qbo, self.apollo if self.cfg.mode != "dry_run" else None,
                         self.alerter, today=day, report_dir=self.tmp / "reports",
                         now=datetime.combine(day, datetime.min.time()) + timedelta(hours=12), **kw)

    def run_range(self, start: date, end: date):
        d = start
        while d <= end:
            self.run(d)
            d += timedelta(days=1)

    def touches(self, account_key=None):
        from sqlalchemy import select
        from hunter_retention.db import touches, tracks
        with self.db.engine.begin() as c:
            q = select(touches.c.touch_key, touches.c.status, touches.c.reason, touches.c.due_date,
                       tracks.c.account_key).join(tracks, tracks.c.id == touches.c.track_id)
            if account_key:
                q = q.where(tracks.c.account_key == account_key)
            return {r.touch_key: dict(r._mapping) for r in c.execute(q)} if account_key else [dict(r._mapping) for r in c.execute(q)]

    def track(self, account_key):
        from sqlalchemy import select
        from hunter_retention.db import tracks
        with self.db.engine.begin() as c:
            r = c.execute(select(tracks).where(tracks.c.account_key == account_key)).mappings().first()
            return dict(r) if r else None


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path).strip_verify()
