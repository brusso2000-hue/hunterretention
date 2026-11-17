"""QuickBooks -> local mirror.

* First run (or after an outage longer than CDC's 30-day window): full backfill via query, and anything
  missing from QBO is marked deleted locally.
* Normal runs: Change Data Capture since the last watermark (minus overlap), which also reports deletes.
* Upserts are keyed by QBO entity type + Id, so re-running is always safe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, insert, select, update

from .db import DB, qbo_customers, qbo_items, qbo_txn_lines, qbo_txns, utcnow
from .qbo import CDC_ENTITIES, SALES_ENTITIES, QBOSource
from .util import parse_date

log = logging.getLogger(__name__)

WATERMARK_KEY = "qbo_cdc_watermark"
CDC_MAX_DAYS = 29          # QBO keeps 30 days; stay inside it
OVERLAP = timedelta(hours=2)
CHUNK = 500


# ------------------------------------------------------------------ parsing
def parse_lines(lines: list[dict]) -> list[dict]:
    out: list[dict] = []

    def walk(items: list[dict]):
        for ln in items or []:
            dt = ln.get("DetailType")
            if dt == "SalesItemLineDetail":
                d = ln.get("SalesItemLineDetail", {}) or {}
                ref = d.get("ItemRef", {}) or {}
                out.append({
                    "item_id": ref.get("value"), "item_name": ref.get("name"),
                    "description": ln.get("Description"),
                    "qty": float(d.get("Qty") or 0), "amount": float(ln.get("Amount") or 0),
                })
            elif dt == "GroupLineDetail":
                walk((ln.get("GroupLineDetail", {}) or {}).get("Line", []))
            # SubTotalLineDetail / DiscountLineDetail / DescriptionOnly are not products
    walk(lines)
    for i, ln in enumerate(out, 1):
        ln["line_no"] = i
    return out


def txn_status(raw: dict) -> str:
    if raw.get("status") == "Deleted":
        return "deleted"
    note = (raw.get("PrivateNote") or "").lower()
    if float(raw.get("TotalAmt") or 0) == 0 and "void" in note:
        return "voided"
    return "active"


# ------------------------------------------------------------------ upserts
def customer_vals(raw: dict) -> dict:
    return dict(
        display_name=raw.get("DisplayName"), company_name=raw.get("CompanyName"),
        given_name=raw.get("GivenName"), family_name=raw.get("FamilyName"),
        email_raw=(raw.get("PrimaryEmailAddr") or {}).get("Address"),
        parent_id=(raw.get("ParentRef") or {}).get("value"),
        active=bool(raw.get("Active", True)), deleted=False,
        last_updated=(raw.get("MetaData") or {}).get("LastUpdatedTime"), raw=raw,
    )


def item_vals(raw: dict) -> dict:
    return dict(name=raw.get("Name"), sku=raw.get("Sku"), description=raw.get("Description"),
                type=raw.get("Type"), active=bool(raw.get("Active", True)), raw=raw)


def txn_vals(txn_type: str, raw: dict) -> dict:
    ship = raw.get("ShipAddr") or {}
    return dict(
        txn_type=txn_type, qbo_id=str(raw["Id"]),
        customer_id=str((raw.get("CustomerRef") or {}).get("value") or ""),
        doc_number=raw.get("DocNumber"),
        txn_date=parse_date(raw.get("TxnDate")), ship_date=parse_date(raw.get("ShipDate")),
        tracking_num=raw.get("TrackingNum"), ship_country=(ship.get("Country") or None),
        total_amt=float(raw.get("TotalAmt") or 0),
        bill_email=(raw.get("BillEmail") or {}).get("Address"),
        private_note=raw.get("PrivateNote"), status=txn_status(raw),
        last_updated=(raw.get("MetaData") or {}).get("LastUpdatedTime"), raw=raw, synced_at=utcnow(),
    )


def upsert_customer(conn, raw: dict) -> None:
    cid = str(raw["Id"])
    if raw.get("status") == "Deleted":
        if conn.execute(select(qbo_customers.c.id).where(qbo_customers.c.id == cid)).first():
            conn.execute(update(qbo_customers).where(qbo_customers.c.id == cid).values(deleted=True))
        return
    vals = customer_vals(raw)
    if conn.execute(select(qbo_customers.c.id).where(qbo_customers.c.id == cid)).first():
        conn.execute(update(qbo_customers).where(qbo_customers.c.id == cid).values(**vals))
    else:
        conn.execute(insert(qbo_customers).values(id=cid, **vals))


def upsert_item(conn, raw: dict) -> None:
    iid = str(raw["Id"])
    if raw.get("status") == "Deleted":
        return
    vals = item_vals(raw)
    if conn.execute(select(qbo_items.c.id).where(qbo_items.c.id == iid)).first():
        conn.execute(update(qbo_items).where(qbo_items.c.id == iid).values(**vals))
    else:
        conn.execute(insert(qbo_items).values(id=iid, **vals))


def upsert_txn(conn, txn_type: str, raw: dict) -> None:
    pk = f"{txn_type}:{raw['Id']}"
    status = txn_status(raw)
    exists = conn.execute(select(qbo_txns.c.pk).where(qbo_txns.c.pk == pk)).first()
    if status == "deleted":
        if exists:
            conn.execute(update(qbo_txns).where(qbo_txns.c.pk == pk).values(status="deleted", synced_at=utcnow()))
        return
    vals = txn_vals(txn_type, raw)
    if exists:
        conn.execute(update(qbo_txns).where(qbo_txns.c.pk == pk).values(**vals))
    else:
        conn.execute(insert(qbo_txns).values(pk=pk, **vals))
    conn.execute(delete(qbo_txn_lines).where(qbo_txn_lines.c.txn_pk == pk))
    lines = parse_lines(raw.get("Line", []) or [])
    if lines:
        conn.execute(insert(qbo_txn_lines), [{"txn_pk": pk, **ln} for ln in lines])


# ------------------------------------------------------------------ sync orchestration
class Syncer:
    def __init__(self, db: DB, source: QBOSource, history_start: str):
        self.db = db
        self.src = source
        self.history_start = history_start

    def run(self, now: datetime | None = None, force_full: bool = False) -> dict:
        now = now or utcnow()
        wm_raw = self.db.get_state(WATERMARK_KEY)
        wm = datetime.fromisoformat(wm_raw) if wm_raw else None
        if force_full or wm is None or now - wm > timedelta(days=CDC_MAX_DAYS):
            stats = self.full_sync()
            stats["mode"] = "full"
        else:
            stats = self.incremental(wm - OVERLAP)
            stats["mode"] = "cdc"
        self.db.set_state(WATERMARK_KEY, now.isoformat())
        return stats

    def full_sync(self) -> dict:
        """Batched: replace-in-chunks so a full history load is a few hundred round trips, not tens of thousands."""
        stats = {"customers": 0, "items": 0, "txns": 0, "marked_deleted": 0}
        seen: dict[str, set[str]] = {e: set() for e in SALES_ENTITIES}

        def flush(table, key_col, rows: list[dict], conn):
            if not rows:
                return
            keys = [r[key_col.name] for r in rows]
            conn.execute(delete(table).where(key_col.in_(keys)))
            conn.execute(insert(table), rows)

        customers = [{"id": str(r["Id"]), **customer_vals(r)} for r in self.src.query_all("Customer", "Active IN (true, false)")]
        items = [{"id": str(r["Id"]), **item_vals(r)} for r in self.src.query_all("Item", "Active IN (true, false)")]
        with self.db.engine.begin() as c:
            for i in range(0, len(customers), CHUNK):
                flush(qbo_customers, qbo_customers.c.id, customers[i:i + CHUNK], c)
            for i in range(0, len(items), CHUNK):
                flush(qbo_items, qbo_items.c.id, items[i:i + CHUNK], c)
        stats["customers"], stats["items"] = len(customers), len(items)

        for ent in SALES_ENTITIES:
            batch, lines = [], []
            for row in self.src.query_all(ent, f"TxnDate >= '{self.history_start}'"):
                pk = f"{ent}:{row['Id']}"
                seen[ent].add(str(row["Id"]))
                batch.append({"pk": pk, **txn_vals(ent, row)})
                lines.extend({"txn_pk": pk, **ln} for ln in parse_lines(row.get("Line", []) or []))
                if len(batch) >= CHUNK:
                    self._flush_txns(batch, lines); stats["txns"] += len(batch); batch, lines = [], []
            if batch:
                self._flush_txns(batch, lines); stats["txns"] += len(batch)

        with self.db.engine.begin() as c:
            # anything we hold that QBO no longer returns was deleted (or merged away)
            for ent in SALES_ENTITIES:
                local = c.execute(select(qbo_txns.c.pk, qbo_txns.c.qbo_id).where(
                    (qbo_txns.c.txn_type == ent) & (qbo_txns.c.status != "deleted")
                    & (qbo_txns.c.txn_date >= parse_date(self.history_start)))).all()
                gone = [pk for pk, qid in local if qid not in seen[ent]]
                for i in range(0, len(gone), CHUNK):
                    c.execute(update(qbo_txns).where(qbo_txns.c.pk.in_(gone[i:i + CHUNK])).values(status="deleted"))
                stats["marked_deleted"] += len(gone)
        log.info("QBO full sync: %s", stats)
        return stats

    def _flush_txns(self, batch: list[dict], lines: list[dict]) -> None:
        pks = [b["pk"] for b in batch]
        with self.db.engine.begin() as c:
            c.execute(delete(qbo_txn_lines).where(qbo_txn_lines.c.txn_pk.in_(pks)))
            c.execute(delete(qbo_txns).where(qbo_txns.c.pk.in_(pks)))
            c.execute(insert(qbo_txns), batch)
            if lines:
                c.execute(insert(qbo_txn_lines), lines)

    def incremental(self, since: datetime) -> dict:
        changed = self.src.cdc(CDC_ENTITIES, since)
        stats = {"customers": 0, "items": 0, "txns": 0}
        with self.db.engine.begin() as c:
            for row in changed.get("Customer", []):
                upsert_customer(c, row); stats["customers"] += 1
            for row in changed.get("Item", []):
                upsert_item(c, row); stats["items"] += 1
            for ent in SALES_ENTITIES:
                for row in changed.get(ent, []):
                    upsert_txn(c, ent, row); stats["txns"] += 1
        log.info("QBO CDC sync since %s: %s", since.isoformat(), stats)
        return stats

    def refresh_customers_live(self, customer_ids: list[str], since_date: str) -> int:
        """Immediate pre-send recheck: pull this account's sales transactions straight from QBO."""
        if not customer_ids:
            return 0
        n = 0
        id_list = ",".join(f"'{cid}'" for cid in customer_ids)
        with self.db.engine.begin() as c:
            for ent in SALES_ENTITIES:
                for row in self.src.query_all(ent, f"CustomerRef IN ({id_list}) AND TxnDate >= '{since_date}'"):
                    upsert_txn(c, ent, row); n += 1
        return n
