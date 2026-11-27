"""Persistent state. SQLAlchemy Core so the same code runs on Postgres (prod) and SQLite (tests/dev)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, Index, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, func, insert, select, update,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- QuickBooks mirror
qbo_customers = Table(
    "qbo_customers", metadata,
    Column("id", String(32), primary_key=True),
    Column("display_name", String(255)),
    Column("company_name", String(255)),
    Column("given_name", String(128)),
    Column("family_name", String(128)),
    Column("email_raw", String(512)),
    Column("parent_id", String(32)),
    Column("active", Boolean, default=True),
    Column("deleted", Boolean, default=False),
    Column("last_updated", String(40)),
    Column("raw", JSON),
)

qbo_items = Table(
    "qbo_items", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(255)),
    Column("sku", String(128)),
    Column("description", Text),
    Column("type", String(40)),
    Column("active", Boolean, default=True),
    Column("raw", JSON),
)

qbo_txns = Table(
    "qbo_txns", metadata,
    Column("pk", String(64), primary_key=True),          # e.g. "Invoice:1234"
    Column("txn_type", String(32), nullable=False),      # Invoice | SalesReceipt | CreditMemo | RefundReceipt
    Column("qbo_id", String(32), nullable=False),
    Column("customer_id", String(32), index=True),
    Column("doc_number", String(64)),
    Column("txn_date", Date),
    Column("ship_date", Date),
    Column("tracking_num", String(128)),
    Column("ship_country", String(64)),
    Column("total_amt", Float),
    Column("bill_email", String(255)),
    Column("private_note", Text),
    Column("status", String(16), default="active"),      # active | voided | deleted
    Column("last_updated", String(40)),
    Column("raw", JSON),
    Column("synced_at", DateTime),
)

qbo_txn_lines = Table(
    "qbo_txn_lines", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("txn_pk", String(64), nullable=False),
    Column("line_no", Integer),
    Column("item_id", String(32)),
    Column("item_name", String(255)),
    Column("description", Text),
    Column("qty", Float),
    Column("amount", Float),
)
Index("ix_lines_txn", qbo_txn_lines.c.txn_pk)

sync_state = Table(
    "sync_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
)

oauth_tokens = Table(
    "oauth_tokens", metadata,
    Column("provider", String(32), primary_key=True),
    Column("refresh_token", Text),
    Column("access_token", Text),
    Column("access_expires_at", DateTime),
    Column("refresh_expires_at", DateTime),
    Column("updated_at", DateTime),
)

# ---------------------------------------------------------------- lifecycle state
tracks = Table(
    "tracks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_key", String(128), nullable=False, index=True),
    # flow: first_time (one per account) or reorder (one per overdue episode, keyed by the last order).
    Column("flow", String(16), nullable=False, default="first_time", server_default="first_time"),
    Column("episode_key", String(64), nullable=False, default="", server_default=""),
    Column("customer_ids", JSON),
    Column("email", String(255)),
    Column("first_name", String(128)),
    Column("last_name", String(128)),
    Column("company", String(255)),
    Column("contact_role", String(24)),      # user | buyer | owner_operator | unknown (picks the email version)
    Column("contact_source", String(32)),    # qbo | inbox | ship_to | po_email | apollo_search | manual
    Column("segment", String(8)),            # A B C D
    Column("subsegment", String(64)),        # gold | nickel | tin | multi | carbitron | ...
    Column("primary_family", String(64)),
    Column("families", JSON),
    Column("applications", JSON),
    Column("first_order_pk", String(64)),
    Column("first_order_date", Date),
    Column("anchor_date", Date),             # estimated delivery date
    Column("anchor_basis", String(64)),
    Column("order_summary", JSON),           # skus, descriptions, qty, amounts
    Column("holdout", Boolean, default=False),
    Column("status", String(24)),            # active | converted | completed | canceled | held | holdout
    Column("status_reason", String(255)),
    Column("apollo_contact_id", String(64)),
    Column("reorder_status", String(32), default="none"),
    Column("first_reorder_date", Date),
    Column("first_reorder_pk", String(64)),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    UniqueConstraint("account_key", "flow", "episode_key", name="uq_track_account_flow_episode"),
)

touches = Table(
    "touches", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("track_id", Integer, nullable=False),
    Column("touch_key", String(16), nullable=False),
    Column("due_date", Date),
    Column("window_end", Date),
    Column("status", String(24), default="scheduled"),
    # scheduled | enrolling | sent | suppressed | expired | canceled | failed | holdout | recalled
    Column("reason", String(255)),
    Column("apollo_sequence_id", String(64)),
    Column("apollo_contact_id", String(64)),
    Column("attempt_count", Integer, default=0),
    Column("enrolled_at", DateTime),
    Column("sent_on", Date),                 # business date of enrollment (job's local "today")
    Column("sent_to_email", String(255)),
    Column("rendered_subject", Text),
    Column("rendered_body", Text),
    Column("updated_at", DateTime),
    UniqueConstraint("track_id", "touch_key", name="uq_touch_track_key"),
)

engagement = Table(
    "engagement", metadata,
    Column("touch_id", Integer, primary_key=True, autoincrement=False),
    Column("delivered", Boolean, default=False),
    Column("opened", Boolean, default=False),
    Column("clicked", Boolean, default=False),
    Column("replied", Boolean, default=False),
    Column("bounced", Boolean, default=False),
    Column("unsubscribed", Boolean, default=False),
    Column("raw", JSON),
    Column("updated_at", DateTime),
)

suppressions = Table(
    "suppressions", metadata,
    Column("email", String(255), primary_key=True),
    Column("reason", String(255)),
    Column("source", String(64)),            # manual | apollo_unsubscribe | bounce | support_hold
    Column("created_at", DateTime),
)

review_queue = Table(
    "review_queue", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(40), nullable=False),
    Column("ref", String(255), nullable=False),
    Column("details", JSON),
    Column("created_at", DateTime),
    Column("resolved_at", DateTime),
    UniqueConstraint("kind", "ref", name="uq_review_kind_ref"),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime),
    Column("run_id", Integer),
    Column("level", String(8)),
    Column("kind", String(48)),
    Column("account_key", String(128)),
    Column("track_id", Integer),
    Column("touch_id", Integer),
    Column("message", Text),
    Column("data", JSON),
)

approvals = Table(
    "approvals", metadata,
    Column("key", String(64), primary_key=True),
    Column("approved_by", String(128)),
    Column("approved_at", DateTime),
    Column("note", Text),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("mode", String(16)),
    Column("status", String(16)),
    Column("stats", JSON),
    Column("error", Text),
)

contacts = Table(
    "contacts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_key", String(128), nullable=False, index=True),
    Column("email", String(255), nullable=False),
    Column("first_name", String(128)),
    Column("last_name", String(128)),
    Column("title", String(255)),
    Column("role", String(24)),              # user | buyer | owner_operator | unknown
    Column("source", String(32)),            # qbo | inbox | ship_to | po_email | apollo_search | manual
    Column("score", Integer),                # 0-100 confidence that this is the right person
    Column("status", String(16)),            # primary | alternate | suggested | rejected
    Column("evidence", JSON),                # short human-readable reasons; never email text
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    UniqueConstraint("account_key", "email", name="uq_contact_account_email"),
)

# Pen registrations from hunterproducts.com/register/. The website writes rows (status "new"); the daily
# job matches each one to an order and a recorded lot, then sends the certificate email or holds it.
registrations = Table(
    "registrations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("first_name", String(128)),
    Column("last_name", String(128)),
    Column("email", String(255), nullable=False, index=True),
    Column("company", String(255)),
    Column("application", Text),                 # "What are you plating?" (optional)
    Column("source", String(32), server_default="web"),
    Column("status", String(24), nullable=False, server_default="new"),
    # new | matched | held | duplicate | rejected
    Column("status_reason", String(255)),
    Column("account_key", String(128)),
    Column("order_pk", String(64)),
    Column("lot_codes", JSON),
    Column("track_id", Integer),
    Column("manual_lot", String(64)),            # set by `registrations resolve --lot` after a person checked
    Column("manual_order_pk", String(64)),
    Column("processed_at", DateTime),
)

apollo_fields = Table(
    "apollo_fields", metadata,
    Column("label", String(128), primary_key=True),
    Column("field_id", String(64)),
    Column("type", String(32)),
)


class DB:
    def __init__(self, url: str):
        # Neon/Supabase/Render hand out postgres:// URLs; SQLAlchemy needs the psycopg3 driver name.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+psycopg://" + url[len(prefix):]
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(url, future=True, pool_pre_ping=True, **kwargs)

    def create_all(self) -> None:
        metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """create_all() adds new tables but not new columns on tables that already exist."""
        from sqlalchemy import inspect, text
        insp = inspect(self.engine)
        have = {c["name"] for c in insp.get_columns("tracks")}
        with self.engine.begin() as c:
            for name, ddl in (("contact_role", "VARCHAR(24)"), ("contact_source", "VARCHAR(32)"),
                              ("flow", "VARCHAR(16) NOT NULL DEFAULT 'first_time'"),
                              ("episode_key", "VARCHAR(64) NOT NULL DEFAULT ''")):
                if name not in have:
                    c.execute(text(f"ALTER TABLE tracks ADD COLUMN {name} {ddl}"))
            if self.engine.dialect.name == "postgresql":
                # Older databases allowed one track per account. The reorder flow needs one first-time track
                # plus one track per overdue episode, so the uniqueness moves to (account, flow, episode).
                for uc in insp.get_unique_constraints("tracks"):
                    if uc.get("column_names") == ["account_key"]:
                        c.execute(text(f'ALTER TABLE tracks DROP CONSTRAINT IF EXISTS "{uc["name"]}"'))
                c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_track_account_flow_episode "
                               "ON tracks (account_key, flow, episode_key)"))

    # ---- small helpers -----------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.engine.begin() as c:
            row = c.execute(select(sync_state.c.value).where(sync_state.c.key == key)).first()
            return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.engine.begin() as c:
            if c.execute(select(sync_state.c.key).where(sync_state.c.key == key)).first():
                c.execute(update(sync_state).where(sync_state.c.key == key).values(value=value))
            else:
                c.execute(insert(sync_state).values(key=key, value=value))

    def log(self, kind: str, message: str, level: str = "info", run_id: int | None = None,
            account_key: str | None = None, track_id: int | None = None,
            touch_id: int | None = None, data: dict | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(events).values(
                ts=utcnow(), run_id=run_id, level=level, kind=kind, account_key=account_key,
                track_id=track_id, touch_id=touch_id, message=message, data=data or {}))

    def add_review(self, kind: str, ref: str, details: dict) -> bool:
        """Insert a review item once. Returns True if newly created."""
        with self.engine.begin() as c:
            row = c.execute(select(review_queue.c.id, review_queue.c.resolved_at).where(
                (review_queue.c.kind == kind) & (review_queue.c.ref == ref))).first()
            if row:
                return False
            c.execute(insert(review_queue).values(kind=kind, ref=ref, details=details, created_at=utcnow()))
            return True

    def is_approved(self, key: str) -> bool:
        with self.engine.begin() as c:
            return c.execute(select(approvals.c.key).where(approvals.c.key == key)).first() is not None
