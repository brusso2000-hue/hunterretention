"""Pen registrations: certificate-of-conformance email and use-by reminder.

A customer scans the card in the pen box and fills in the form at hunterproducts.com/register/. The
website writes one row to `registrations`. Each daily run then, per row:

  1. finds the customer's account (exact email, else company email domain)
  2. finds their most recent order that contains plating pens
  3. reads the lot number RECORDED on that order and looks it up in config/lots.yaml
  4. starts a small track: `c1` (certificate email, now) and `x1` (reminder before the use-by date)

The rule that matters: a lot number is never guessed. It is only used when it was written on the
QuickBooks order (or entered by a person with `registrations resolve --lot`) AND it exists in the lot
registry with a use-by date and a certificate document. Anything else is held for a person, with the
reason, and nothing is sent. A certificate has to be true.

Sending goes through the normal dispatcher, so modes (dry_run / sandbox / live), production approval,
unsubscribes and bounces all apply. Because the customer asked for this email, the marketing-only rules
(frequency caps, key-account rule, "already in another sequence") do not.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import insert, select, update

from .classify import LIFECYCLE, Classifier
from .config import Config
from .db import DB, qbo_txns, registrations, review_queue, touches, tracks, utcnow
from .identity import Account
from .util import EMAIL_RE, email_domain, parse_date
from .world import Order, World

log = logging.getLogger(__name__)

PEN_FAMILY = "micro_metallizer"
OPEN = ("new", "held")
# "Lot HP-2611-G24", "LOT#: HP-2611-G24", "Lot No. HP-2611-G24"
LOT_RE = re.compile(r"\blot\s*(?:no\.?|number|num\.?)?\s*[#:]?\s*([A-Za-z0-9][A-Za-z0-9-]{2,30})", re.I)


@dataclass
class Lot:
    code: str
    skus: tuple[str, ...]
    received: date | None
    use_by: date | None
    coc_url: str
    problems: list[str] = field(default_factory=list)


class LotRegistry:
    """config/lots.yaml. One entry per inbound shipment of pens."""

    def __init__(self, cfg: Config):
        self.lots: dict[str, Lot] = {}
        for raw in (cfg.lots or {}).get("lots") or []:
            code = str(raw.get("code") or "").strip().upper()
            if not code:
                continue
            lot = Lot(code=code, skus=tuple(str(s).strip().upper() for s in (raw.get("skus") or [])),
                      received=parse_date(raw.get("received")), use_by=parse_date(raw.get("use_by")),
                      coc_url=str(raw.get("coc_url") or "").strip())
            if not lot.use_by:
                lot.problems.append("no use_by date")
            if not lot.coc_url:
                lot.problems.append("no certificate document (coc_url)")
            self.lots[code] = lot

    def get(self, code: str | None) -> Lot | None:
        return self.lots.get((code or "").strip().upper())


def recorded_lots(order: Order, raw: dict | None) -> list[tuple[str, str | None]]:
    """Lot codes written on a QuickBooks order, as (code, sku or None). Looked for on each pen line's
    description first (ties the lot to that product), then on order-level fields: a custom field whose
    name contains "lot", the message on the invoice, and the private note."""
    found: list[tuple[str, str | None]] = []

    def add(code: str, sku: str | None) -> None:
        code = code.upper()
        if (code, sku) not in found:
            found.append((code, sku))

    for ln in order.lines:
        for m in LOT_RE.finditer(ln.description or ""):
            add(m.group(1), (ln.cls.sku or "").upper() or None)
    raw = raw or {}
    for cf in raw.get("CustomField") or []:
        if "lot" in (cf.get("Name") or "").lower() and (cf.get("StringValue") or "").strip():
            for part in re.split(r"[,;/\s]+", cf["StringValue"].strip()):
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,30}", part):
                    add(part, None)
    memo = (raw.get("CustomerMemo") or {}).get("value") or ""
    for text in (memo, raw.get("PrivateNote") or ""):
        for m in LOT_RE.finditer(text):
            add(m.group(1), None)
    return found


class Hold(Exception):
    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.reason, self.details = reason, details


class RegistrationPlanner:
    def __init__(self, db: DB, cfg: Config, classifier: Classifier, world: World, today: date,
                 run_id: int | None = None):
        self.db, self.cfg, self.classifier, self.world, self.today, self.run_id = db, cfg, classifier, world, today, run_id
        self.enabled = bool(cfg.s("registration.enabled", False))
        self.max_age = int(cfg.s("registration.max_order_age_days", 400))
        self.freemail = set(cfg.s("identity.freemail_domains", []))
        self.lots = LotRegistry(cfg)
        self.stats = {"new": 0, "started": 0, "held": 0, "held_new": 0, "duplicate": 0, "rejected": 0}

    # ------------------------------------------------------------------ main
    def plan(self) -> dict:
        if not self.enabled:
            return self.stats
        self._cancel_reminders_after_reorder()
        with self.db.engine.begin() as c:
            rows = [dict(r) for r in c.execute(select(registrations).where(
                registrations.c.status.in_(OPEN)).order_by(registrations.c.id)).mappings()]
        for reg in rows:
            was_new = reg["status"] == "new"
            self.stats["new"] += 1 if was_new else 0
            try:
                self._one(reg)
            except Hold as h:
                self._hold(reg, h, was_new)
            except Exception as exc:               # one bad row must not stop the run
                log.exception("registration %s failed", reg["id"])
                self.db.log("registration_error", str(exc), "error", self.run_id, data={"registration": reg["id"]})
        return self.stats

    def _one(self, reg: dict) -> None:
        email = (reg.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            self._close(reg, "rejected", "invalid_email")
            self.stats["rejected"] += 1
            return
        acct, order = self._match(reg, email)
        if self._already_sent(email, order.pk):
            self._close(reg, "duplicate", f"already registered for order {order.doc_number or order.pk}",
                        account_key=acct.key, order_pk=order.pk)
            self.stats["duplicate"] += 1
            return
        lots = self._lots_for(reg, order)
        self._start(reg, email, acct, order, lots)

    # ------------------------------------------------------------------ matching
    def _match(self, reg: dict, email: str) -> tuple[Account, Order]:
        accounts = [a for a in self.world.accounts.values() if email in a.emails or email in a.role_emails]
        if not accounts:
            dom = email_domain(email)
            if dom and dom not in self.freemail:
                accounts = [a for a in self.world.accounts.values() if dom in a.domains]
        if not accounts:
            raise Hold("registration_no_account", why="no customer in QuickBooks has this email or email domain "
                       "(bought through a distributor, or used a personal address?)")
        if reg.get("manual_order_pk"):
            # A person looked and named the order (`registrations resolve --order`).
            for a in self.world.accounts.values():
                for o in self.world.orders.get(a.key, []):
                    if o.pk == reg["manual_order_pk"]:
                        return a, o
            raise Hold("registration_order_not_found", order=reg["manual_order_pk"])
        since = self.today - timedelta(days=self.max_age)
        options: list[tuple[Account, Order]] = []
        for a in accounts:
            pens = [o for o in self.world.orders.get(a.key, []) if since <= o.date <= self.today and _pen_lines(o)]
            if pens:
                options.append((a, pens[-1]))
        if not options:
            raise Hold("registration_no_pen_order", accounts=[a.company for a in accounts],
                       why=f"no order with plating pens in the last {self.max_age} days")
        if len(options) > 1:
            # Several QuickBooks customers share this email domain (a company with several plants). Which
            # plant's order this pen came from is a fact, not something to guess.
            raise Hold("registration_ambiguous_account", why="more than one customer record matches this domain",
                       options=[{"company": a.company, "order": o.pk, "doc_number": o.doc_number, "date": str(o.date)}
                                for a, o in options])
        return options[0]

    def _lots_for(self, reg: dict, order: Order) -> list[dict]:
        pens = _pen_lines(order)
        pen_skus = {(ln.cls.sku or "").upper(): ln for ln in pens}
        if reg.get("manual_lot"):
            pairs: list[tuple[str, str | None]] = [(str(reg["manual_lot"]).upper(), None)]
        else:
            with self.db.engine.begin() as c:
                raw = c.execute(select(qbo_txns.c.raw).where(qbo_txns.c.pk == order.pk)).scalar()
            pairs = recorded_lots(order, raw)
        order_ref = {"order": order.pk, "doc_number": order.doc_number, "date": str(order.date),
                     "pens": [f"{ln.qty:g} x {ln.cls.display} ({ln.cls.sku})" for ln in pens]}
        if not pairs:
            raise Hold("registration_no_lot_recorded", why="no lot number is written on this order", **order_ref)
        out: dict[str, dict] = {}
        for code, sku in pairs:
            lot = self.lots.get(code)
            if lot is None:
                raise Hold("registration_unknown_lot", lot=code, why="this lot is not in config/lots.yaml", **order_ref)
            if lot.problems:
                raise Hold("registration_lot_incomplete", lot=code, why="; ".join(lot.problems), **order_ref)
            covered = [s for s in ([sku] if sku else list(pen_skus)) if s and (not lot.skus or s in lot.skus)]
            if not covered:
                raise Hold("registration_lot_product_mismatch", lot=code, lot_skus=list(lot.skus),
                           why="the lot on this order is registered for different products", **order_ref)
            d = out.setdefault(code, {"code": code, "use_by": str(lot.use_by), "use_by_long": _long(lot.use_by),
                                      "coc_url": lot.coc_url, "products": []})
            for s in covered:
                name = pen_skus[s].cls.display if s in pen_skus else s
                if name not in d["products"]:
                    d["products"].append(name)
        return sorted(out.values(), key=lambda d: d["use_by"])

    def _already_sent(self, email: str, order_pk: str) -> bool:
        with self.db.engine.begin() as c:
            return c.execute(select(tracks.c.id).where(
                (tracks.c.flow == "registration") & (tracks.c.email == email)
                & (tracks.c.first_order_pk == order_pk))).first() is not None

    # ------------------------------------------------------------------ writes
    def _start(self, reg: dict, email: str, acct: Account, order: Order, lots: list[dict]) -> None:
        pens = _pen_lines(order)
        lines = [{"sku": ln.cls.sku, "display": ln.cls.display, "qty": ln.qty or 0, "amount": ln.amount or 0,
                  "family": ln.cls.family, "role": ln.cls.role, "applications": list(ln.cls.applications),
                  "url": ln.cls.url} for ln in pens]
        first_use_by = parse_date(lots[0]["use_by"])
        vals = dict(
            account_key=acct.key, flow="registration", episode_key=f"reg:{reg['id']}", customer_ids=acct.customer_ids,
            email=email, first_name=(reg.get("first_name") or "").strip()[:128],
            last_name=(reg.get("last_name") or "").strip()[:128],
            company=(reg.get("company") or "").strip()[:255] or acct.company,
            contact_role="user", contact_source="registration", segment="P", subsegment="pen",
            primary_family=PEN_FAMILY, families=[PEN_FAMILY], applications=[],
            first_order_pk=order.pk, first_order_date=order.date, anchor_date=self.today,
            anchor_basis="pen_registration",
            order_summary={"orders": [order.pk], "lines": lines, "po_number": order.po_number,
                           "doc_number": order.doc_number, "last_order_date": str(order.date),
                           "registration": {"id": reg["id"], "lots": lots, "many": len(lots) > 1,
                                            "first_use_by": lots[0]["use_by"],
                                            "first_use_by_long": lots[0]["use_by_long"],
                                            "order_label": _order_label(order),
                                            "application": (reg.get("application") or "").strip()[:500]}},
            holdout=False, status="active", status_reason=None, reorder_status="none",
            created_at=utcnow(), updated_at=utcnow())
        with self.db.engine.begin() as c:
            tid = c.execute(insert(tracks).values(**vals)).inserted_primary_key[0]
            for t in self.cfg.lc("tracks.P.touches", []) or []:
                due = self._due(t, first_use_by)
                if due is None:
                    continue
                c.execute(insert(touches).values(
                    track_id=tid, touch_key=t["key"], due_date=due,
                    window_end=due + timedelta(days=int(t.get("window_days", 14))),
                    status="scheduled", attempt_count=0, updated_at=utcnow()))
            c.execute(update(registrations).where(registrations.c.id == reg["id"]).values(
                status="matched", status_reason=None, account_key=acct.key, order_pk=order.pk,
                lot_codes=[d["code"] for d in lots], track_id=tid, processed_at=utcnow()))
            # an earlier hold on this registration is now answered
            c.execute(update(review_queue).where((review_queue.c.ref == f"registration:{reg['id']}")
                                                 & review_queue.c.resolved_at.is_(None)).values(resolved_at=utcnow()))
        self.stats["started"] += 1
        self.db.log("registration_matched",
                    f"{email} ({acct.company}): order {order.doc_number or order.pk}, lot {', '.join(d['code'] for d in lots)}",
                    run_id=self.run_id, account_key=acct.key, track_id=tid, data={"registration": reg["id"]})

    def _due(self, t: dict, use_by: date | None) -> date | None:
        if t.get("offset") != "use_by":
            return self.today + timedelta(days=int(t.get("offset_days", 0)))
        if not use_by:
            return None
        due = use_by - timedelta(days=int(t.get("lead_days", 60)))
        earliest = self.today + timedelta(days=int(t.get("min_days_after_registration", 7)))
        if due < earliest:
            # Registered late in the pen's life: still remind them, but not on top of the certificate email,
            # and not at all once there are under two weeks left.
            due = earliest
        return due if (use_by - due).days >= int(t.get("min_days_before_use_by", 14)) else None

    def _cancel_reminders_after_reorder(self) -> None:
        """They already bought more pens since registering: the use-by reminder would only be noise."""
        with self.db.engine.begin() as c:
            rows = [dict(r) for r in c.execute(
                select(touches.c.id, tracks.c.account_key, tracks.c.first_order_date)
                .join(tracks, tracks.c.id == touches.c.track_id)
                .where((tracks.c.flow == "registration") & (touches.c.touch_key == "x1")
                       & (touches.c.status == "scheduled"))).mappings()]
        for r in rows:
            # the registered order was their latest pen order at the time, so anything after it is a reorder
            newer = [o for o in self.world.orders.get(r["account_key"], [])
                     if r["first_order_date"] < o.date <= self.today and _pen_lines(o)]
            if newer:
                with self.db.engine.begin() as c:
                    c.execute(update(touches).where(touches.c.id == r["id"]).values(
                        status="suppressed", reason="reordered_pens_since_registration", updated_at=utcnow()))

    def _hold(self, reg: dict, h: Hold, was_new: bool) -> None:
        with self.db.engine.begin() as c:
            c.execute(update(registrations).where(registrations.c.id == reg["id"]).values(
                status="held", status_reason=h.reason, processed_at=utcnow()))
        self.stats["held"] += 1
        created = self.db.add_review(h.reason, f"registration:{reg['id']}", {
            "registration": reg["id"], "email": reg.get("email"), "company": reg.get("company"),
            "name": f"{reg.get('first_name') or ''} {reg.get('last_name') or ''}".strip(), **h.details})
        if created or (was_new and reg.get("status_reason") != h.reason):
            self.stats["held_new"] += 1
            self.db.log("registration_held", f"{reg.get('email')}: {h.reason}", "warn", self.run_id,
                        data={"registration": reg["id"], **h.details})

    def _close(self, reg: dict, status: str, reason: str, **extra) -> None:
        with self.db.engine.begin() as c:
            c.execute(update(registrations).where(registrations.c.id == reg["id"]).values(
                status=status, status_reason=reason[:255], processed_at=utcnow(), **extra))


def _pen_lines(order: Order) -> list:
    """Plating pens, degreaser pens, and the kits that ship with pens in the box."""
    return [ln for ln in order.lines if ln.cls.status == LIFECYCLE and ln.cls.family == PEN_FAMILY
            and ln.cls.role in ("consumable", "prep", "equipment")]


def _long(d: date | None) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else ""


def _order_label(order: Order) -> str:
    """How to name the order to the customer: their PO number when there is one, else our invoice number."""
    po = (order.po_number or "").strip()
    if po:
        return po if po.upper().startswith(("PO", "P.O")) else f"PO {po}"
    return f"invoice {order.doc_number}" if order.doc_number else ""
