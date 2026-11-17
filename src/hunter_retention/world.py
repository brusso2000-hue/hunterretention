"""Builds an in-memory view of accounts and their orders from the local QBO mirror.

Hunter's volume (thousands of transactions, not millions) makes a full rebuild each run cheap, and a full
rebuild means every decision is recomputed from current QuickBooks truth: edits, voids, deletes, merges and
late imports are all picked up without special-case bookkeeping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select

from .classify import IGNORED, LIFECYCLE, UNCLASSIFIED, Classifier, Line, OrderClass
from .config import Config
from .db import DB, qbo_customers, qbo_txn_lines, qbo_txns
from .identity import Account, resolve_accounts
from .util import parse_emails

ORDER_TYPES = ("Invoice", "SalesReceipt")
CREDIT_TYPES = ("CreditMemo", "RefundReceipt")


@dataclass
class Order:
    pk: str
    txn_type: str
    customer_id: str
    date: date
    ship_date: date | None
    ship_country: str | None
    tracking_num: str | None
    total: float
    lines: list[Line]
    product_amount: float
    refunded: bool = False
    cls: OrderClass | None = None

    def lines_of(self, family: str, roles: tuple[str, ...] | None = None) -> list[Line]:
        return [ln for ln in self.lines if ln.cls.status == LIFECYCLE and ln.cls.family == family
                and (roles is None or ln.cls.role in roles)]


@dataclass
class FirstPurchase:
    orders: list[Order]
    cls: OrderClass

    @property
    def primary(self) -> Order:
        return self.orders[0]

    @property
    def date(self) -> date:
        return self.orders[0].date

    @property
    def lines(self) -> list[Line]:
        return [ln for o in self.orders for ln in o.lines]

    @property
    def pks(self) -> list[str]:
        return [o.pk for o in self.orders]


@dataclass
class World:
    accounts: dict[str, Account]
    cust_to_acct: dict[str, str]
    orders: dict[str, list[Order]] = field(default_factory=dict)        # account -> qualifying orders, sorted
    all_orders: dict[str, list[Order]] = field(default_factory=dict)    # incl. refunded/zero, for audit


def load_world(db: DB, cfg: Config, classifier: Classifier) -> World:
    role_patterns = cfg.s("identity.role_address_patterns", [])
    freemail = set(cfg.s("identity.freemail_domains", []))
    min_amount = float(cfg.s("eligibility.min_order_amount", 0.01))
    ignored_counts = bool(cfg.s("eligibility.ignored_products_count_as_history", True))
    refund_window = int(cfg.s("eligibility.refund_window_days", 120))
    refund_threshold = float(cfg.s("eligibility.full_refund_threshold", 0.9))

    with db.engine.begin() as c:
        customers = [dict(r) for r in c.execute(
            select(qbo_customers).where(qbo_customers.c.deleted.is_(False))).mappings()]
        txns = [dict(r) for r in c.execute(
            select(qbo_txns).where(qbo_txns.c.status == "active")).mappings()]
        lines_rows = [dict(r) for r in c.execute(select(qbo_txn_lines)).mappings()]

    lines_by_txn: dict[str, list[dict]] = {}
    for ln in lines_rows:
        lines_by_txn.setdefault(ln["txn_pk"], []).append(ln)

    extra_emails: dict[str, list[str]] = {}
    for t in txns:
        if t["bill_email"] and t["customer_id"]:
            lst = extra_emails.setdefault(t["customer_id"], [])
            for e in parse_emails(t["bill_email"]):
                if e not in lst:
                    lst.append(e)

    accounts, c2a = resolve_accounts(customers, role_patterns, freemail, extra_emails)
    world = World(accounts, c2a)

    credits_by_acct: dict[str, list[dict]] = {}
    for t in txns:
        acct = c2a.get(t["customer_id"])
        if not acct or t["txn_date"] is None:
            continue
        if t["txn_type"] in CREDIT_TYPES:
            credits_by_acct.setdefault(acct, []).append(t)
            continue
        if t["txn_type"] not in ORDER_TYPES:
            continue
        lines = []
        for ln in sorted(lines_by_txn.get(t["pk"], []), key=lambda x: x["line_no"] or 0):
            cls = classifier.classify_item(ln["item_id"], ln["item_name"])
            lines.append(Line(ln["item_id"], ln["item_name"], ln["description"], ln["qty"] or 0,
                              ln["amount"] or 0, cls))
        countable = {LIFECYCLE, UNCLASSIFIED} | ({IGNORED} if ignored_counts else set())
        product_amount = sum(ln.amount for ln in lines if ln.cls.status in countable)
        o = Order(t["pk"], t["txn_type"], t["customer_id"], t["txn_date"], t["ship_date"],
                  t["ship_country"], t["tracking_num"], t["total_amt"] or 0, lines, product_amount)
        world.all_orders.setdefault(acct, []).append(o)

    for acct, orders in world.all_orders.items():
        orders.sort(key=lambda o: (o.date, _pk_sort(o.pk)))
        credits = sorted(credits_by_acct.get(acct, []), key=lambda t: t["txn_date"])
        used: set[str] = set()
        for o in orders:
            for cr in credits:
                if cr["pk"] in used:
                    continue
                if o.date <= cr["txn_date"] <= o.date + timedelta(days=refund_window) and o.total > 0 \
                        and (cr["total_amt"] or 0) >= refund_threshold * o.total:
                    o.refunded = True
                    used.add(cr["pk"])
                    break
        world.orders[acct] = [o for o in orders if o.product_amount >= min_amount and not o.refunded]
    return world


def _pk_sort(pk: str):
    t, _, i = pk.partition(":")
    return (t, int(i) if i.isdigit() else 0)


def first_purchase(world: World, acct: str, classifier: Classifier, merge_days: int) -> FirstPurchase | None:
    orders = world.orders.get(acct) or []
    if not orders:
        return None
    first = orders[0]
    group = [o for o in orders if o.date <= first.date + timedelta(days=merge_days)]
    cls = classifier.classify_lines([ln for o in group for ln in o.lines])
    return FirstPurchase(group, cls)


def reorders_after(world: World, acct: str, fp: FirstPurchase) -> list[Order]:
    pks = set(fp.pks)
    return [o for o in world.orders.get(acct, []) if o.pk not in pks and o.date >= fp.date]


def cohort_replenishment_days(world: World, classifier: Classifier, merge_days: int) -> dict[str, list[int]]:
    """Observed days from first purchase to first same-family consumable reorder, per family, all history."""
    out: dict[str, list[int]] = {}
    for acct in world.orders:
        fp = first_purchase(world, acct, classifier, merge_days)
        if not fp or fp.cls.primary_family is None:
            continue
        fam = fp.cls.primary_family
        for o in reorders_after(world, acct, fp):
            if o.lines_of(fam, ("consumable",)):
                out.setdefault(fam, []).append((o.date - fp.date).days)
                break
    return out


def median_or_none(values: list[int], min_n: int) -> float | None:
    return float(median(values)) if len(values) >= min_n else None
