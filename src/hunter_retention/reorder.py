"""Reorder flow: nudge existing customers who are taking longer than usual to reorder.

Every repeat customer has a rhythm: the usual number of days between their orders. When a customer goes
`trigger_multiple` times their usual gap without ordering, an "episode" starts: up to three short emails
(r1, r2, r3) listing what they bought last time. Any new order ends the episode at once.

Episodes are rows in the same `tracks` table as the first-time flow (flow = "reorder", segment = "R",
episode_key = the last order before the gap), so sending, dry run, sandbox, the live approval gate,
frequency caps, reply handling and suppression are the exact same code path.

Not automated, flagged for a person instead (review list):
  * key accounts: bought `personal_note_min_revenue` or more in the last 12 months
  * irregular buyers: gaps too uneven for a "usual" gap to mean anything
  * long gone: more than `long_gone_multiple` times their usual gap (that is a win-back, not a reminder)
Skipped entirely: customers still in the first-time flow, excluded domains, fewer than `min_orders` orders.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import insert, select, update

from .classify import LIFECYCLE, Classifier
from .config import Config
from .db import DB, touches, tracks, utcnow
from .identity import Account
from .world import Order, World

log = logging.getLogger(__name__)

OPEN = ("active", "held")


@dataclass
class Rhythm:
    """What we know about how one account buys."""
    account_key: str
    events: list[date]                 # purchase dates, orders within merge_days counted once
    last: Order
    usual_gap: int                     # median days between recent orders
    days_since: int
    trigger_on: date                   # the day they became overdue
    revenue_12m: float
    irregular: bool
    decision: str = ""                 # start | not_due | long_gone | key_account | irregular | first_time_flow | ...
    detail: str = ""
    lines: list[dict] = field(default_factory=list)   # what they bought last time, for the email


class ReorderPlanner:
    def __init__(self, db: DB, cfg: Config, classifier: Classifier, world: World, today: date,
                 run_id: int | None = None):
        self.db, self.cfg, self.clf, self.world, self.today, self.run_id = db, cfg, classifier, world, today, run_id
        r = cfg.s("reorder", {}) or {}
        self.enabled = bool(r.get("enabled", True))
        self.start_date = _d(r.get("start_date")) or cfg.program_start_date
        self.min_orders = int(r.get("min_orders", 3))
        self.recent_gaps = int(r.get("recent_gaps", 6))
        self.min_gap = int(r.get("min_usual_gap_days", 21))
        self.max_gap = int(r.get("max_usual_gap_days", 400))
        self.trigger_multiple = float(r.get("trigger_multiple", 1.3))
        self.trigger_extra_days = int(r.get("trigger_extra_days", 0))
        self.gap_basis = str(r.get("gap_basis", "median"))     # median of recent gaps | last (their last gap only)
        self.long_gone_multiple = float(r.get("long_gone_multiple", 3.0))
        self.key_revenue = float(r.get("personal_note_min_revenue", 10000))
        self.irregular_cv = float(r.get("irregular_cv", 1.0))
        self.exclude_domains = {d.lower() for d in (r.get("exclude_domains") or [])}
        self.exclude_accounts = set(r.get("exclude_accounts") or [])
        self.merge_days = int(cfg.lc("first_order_merge_days", 2))
        self.stats = {"started": 0, "held": 0, "converted": 0, "flagged_key_account": 0, "flagged_irregular": 0,
                      "flagged_long_gone": 0, "canceled": 0}

    # ------------------------------------------------------------------ analysis (no writes)
    def rhythm(self, key: str) -> Rhythm | None:
        """The account's buying rhythm, or None when there are too few orders to have one."""
        orders = [o for o in (self.world.orders.get(key) or []) if o.date <= self.today]
        if not orders:
            return None
        events: list[date] = []
        for o in orders:                       # several invoices for one shipment count as one purchase
            if not events or (o.date - events[-1]).days > self.merge_days:
                events.append(o.date)
        if len(events) < self.min_orders:
            return None
        gaps = [(b - a).days for a, b in zip(events, events[1:])][-self.recent_gaps:]
        basis = gaps[-1] if self.gap_basis == "last" else statistics.median(gaps)
        usual = max(self.min_gap, int(round(basis)))
        mean = statistics.fmean(gaps)
        cv = (statistics.pstdev(gaps) / mean) if mean and len(gaps) >= 3 else 0.0
        last = orders[-1]
        year_ago = self.today - timedelta(days=365)
        rev = sum(o.product_amount for o in orders if o.date >= year_ago)
        return Rhythm(key, events, last, usual, (self.today - last.date).days,
                      last.date + timedelta(days=math.ceil(usual * self.trigger_multiple) + self.trigger_extra_days),
                      rev,
                      irregular=cv > self.irregular_cv)

    def decide(self, acct: Account, first_time_open: set[str], have_episode: set[tuple[str, str]]) -> Rhythm | None:
        """Decide what should happen for one account today. Pure: writes nothing."""
        rh = self.rhythm(acct.key)
        if rh is None:
            return None
        rh.lines = self._last_lines(acct.key)
        if (acct.key, rh.last.pk) in have_episode:
            rh.decision = "already_handled"
        elif acct.key in self.exclude_accounts or (acct.domains & self.exclude_domains):
            rh.decision, rh.detail = "excluded", "on the exclude list (handled by other outreach)"
        elif acct.key in first_time_open:
            rh.decision, rh.detail = "first_time_flow", "still in the first-time buyer emails"
        elif self.today < rh.trigger_on:
            rh.decision = "not_due"
        elif rh.usual_gap > self.max_gap:
            rh.decision, rh.detail = "too_infrequent", f"orders about every {rh.usual_gap} days"
        elif rh.days_since > rh.usual_gap * self.long_gone_multiple:
            rh.decision, rh.detail = "long_gone", f"{rh.days_since} days since last order, usual gap {rh.usual_gap}"
        elif rh.revenue_12m >= self.key_revenue:
            rh.decision, rh.detail = "key_account", f"${rh.revenue_12m:,.0f} in the last 12 months"
        elif rh.irregular:
            rh.decision, rh.detail = "irregular", "order timing too uneven to automate"
        elif not rh.lines:
            rh.decision, rh.detail = "no_products", "last order had no recognised products"
        else:
            rh.decision = "start"
        return rh

    def due_accounts(self) -> list[str]:
        """Accounts that would start an episode today. The contact lookup runs for these first."""
        if not self.enabled or self.today < self.start_date:
            return []
        ft, eps = self._first_time_open(), self._episodes()
        out = []
        for key, acct in self.world.accounts.items():
            rh = self.decide(acct, ft, eps)
            if rh and rh.decision == "start":
                out.append(key)
        return out

    # ------------------------------------------------------------------ daily entry
    def plan(self) -> dict:
        if not self.enabled:
            return self.stats
        self._close_on_new_order()
        self._recheck_holds()
        if self.today < self.start_date:
            return self.stats
        ft, eps = self._first_time_open(), self._episodes()
        for key, acct in self.world.accounts.items():
            try:
                rh = self.decide(acct, ft, eps)
                if rh is None:
                    continue
                if rh.decision == "start":
                    self._start(acct, rh)
                elif rh.decision in ("key_account", "irregular", "long_gone"):
                    self._flag(acct, rh)
            except Exception as exc:               # one bad account must not stop the run
                log.exception("reorder planning failed for %s", key)
                self.db.log("reorder_plan_error", str(exc), "error", self.run_id, account_key=key)
        return self.stats

    def plan_one(self, account_key: str) -> None:
        """Live recheck just before a send: did they order in the last few minutes?"""
        self._close_on_new_order(only=account_key)

    # ------------------------------------------------------------------ writes
    def _start(self, acct: Account, rh: Rhythm) -> None:
        status, reason = "active", None
        if not acct.contact_email:
            status, reason = "held", ("role_email_only" if acct.role_emails else "missing_email")
        fam = _primary_family(rh.lines)
        vals = dict(
            account_key=acct.key, flow="reorder", episode_key=rh.last.pk, customer_ids=acct.customer_ids,
            email=acct.contact_email, first_name=acct.first_name, last_name=acct.last_name, company=acct.company,
            contact_role=acct.contact_role, contact_source=acct.contact_source,
            segment="R", subsegment=fam, primary_family=fam, families=sorted({ln["family"] for ln in rh.lines}),
            applications=[], first_order_pk=rh.last.pk, first_order_date=rh.last.date,
            anchor_date=self.today, anchor_basis="overdue_vs_usual_gap",
            order_summary={"orders": [rh.last.pk], "lines": rh.lines, "po_number": rh.last.po_number,
                           "doc_number": rh.last.doc_number, "last_order_date": str(rh.last.date),
                           "usual_gap_days": rh.usual_gap, "days_since": rh.days_since,
                           "order_count": len(rh.events), "revenue_12m": round(rh.revenue_12m, 2)},
            holdout=False, status=status, status_reason=reason, reorder_status="none",
            created_at=utcnow(), updated_at=utcnow())
        plan = self.cfg.lc("tracks.R.touches", []) or []
        with self.db.engine.begin() as c:
            tid = c.execute(insert(tracks).values(**vals)).inserted_primary_key[0]
            for t in plan:
                due = self.today + timedelta(days=int(t.get("offset_days", 0)))
                c.execute(insert(touches).values(
                    track_id=tid, touch_key=t["key"], due_date=due,
                    window_end=due + timedelta(days=int(t.get("window_days", 14))),
                    status="scheduled", attempt_count=0, updated_at=utcnow()))
        self.stats["started"] += 1
        if status == "held":
            self.stats["held"] += 1
            self.db.add_review(reason, acct.key, {"company": acct.company, "emails": acct.emails,
                                                  "role_emails": acct.role_emails, "track_id": tid,
                                                  "flow": "reorder"})
        self.db.log("reorder_episode_started",
                    f"{acct.company}: {rh.days_since} days since last order, usual gap {rh.usual_gap} ({status})",
                    run_id=self.run_id, account_key=acct.key, track_id=tid)

    def _flag(self, acct: Account, rh: Rhythm) -> None:
        kind = {"key_account": "reorder_key_account", "irregular": "reorder_irregular",
                "long_gone": "reorder_long_gone"}[rh.decision]
        # one flag per account per last order: a new order and a new gap can flag again
        if self.db.add_review(kind, f"{acct.key}|{rh.last.pk}", {
                "account": acct.key, "company": acct.company, "contact": acct.contact_email,
                "last_order": str(rh.last.date), "days_since": rh.days_since, "usual_gap_days": rh.usual_gap,
                "revenue_12m": round(rh.revenue_12m, 2), "why": rh.detail,
                "last_bought": [f"{ln['qty']:g} x {ln['display']} ({ln['sku']})" for ln in rh.lines[:6]]}):
            self.stats[f"flagged_{rh.decision}"] += 1

    def _close_on_new_order(self, only: str | None = None) -> None:
        """Any order after the episode's last order ends it: remaining emails are suppressed."""
        with self.db.engine.begin() as c:
            q = select(tracks).where((tracks.c.flow == "reorder") & (tracks.c.status.in_(OPEN)))
            if only:
                q = q.where(tracks.c.account_key == only)
            open_eps = [dict(r) for r in c.execute(q).mappings()]
        for tr in open_eps:
            acct_orders = self.world.orders.get(tr["account_key"])
            if acct_orders is None:
                self._end(tr, "canceled", "account_removed_from_quickbooks")
                self.stats["canceled"] += 1
                continue
            newer = [o for o in acct_orders if tr["first_order_date"] < o.date <= self.today]
            if newer:
                self._end(tr, "converted", "reorder:any_purchase",
                          first_reorder_pk=newer[0].pk, first_reorder_date=newer[0].date,
                          reorder_status="reordered")
                self.stats["converted"] += 1
                self.db.log("reorder_detected", f"{tr['company']} ordered again; reorder reminders stopped",
                            run_id=self.run_id, account_key=tr["account_key"], track_id=tr["id"],
                            data={"order": newer[0].pk})

    def _recheck_holds(self) -> None:
        with self.db.engine.begin() as c:
            held = [dict(r) for r in c.execute(select(tracks).where(
                (tracks.c.flow == "reorder") & (tracks.c.status == "held"))).mappings()]
        for tr in held:
            acct = self.world.accounts.get(tr["account_key"])
            if acct and acct.contact_email and tr["status_reason"] in ("role_email_only", "missing_email"):
                with self.db.engine.begin() as c:
                    c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                        status="active", status_reason="contact_fixed", email=acct.contact_email,
                        first_name=acct.first_name, last_name=acct.last_name, contact_role=acct.contact_role,
                        contact_source=acct.contact_source, updated_at=utcnow()))

    def _end(self, tr: dict, status: str, reason: str, **extra) -> None:
        with self.db.engine.begin() as c:
            c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                      .values(status="suppressed" if status == "converted" else "canceled", reason=reason,
                              updated_at=utcnow()))
            c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                status=status, status_reason=reason, updated_at=utcnow(), **extra))

    # ------------------------------------------------------------------ lookups
    def _first_time_open(self) -> set[str]:
        """Accounts the first-time flow still owns: an open track, or a first purchase inside the program."""
        with self.db.engine.begin() as c:
            # ...and for 150 days after the first order either way, so a new customer who reorders once
            # is not handed a "you're overdue" email while the first-time emails are still fresh.
            fresh = self.today - timedelta(days=150)
            keys = {r[0] for r in c.execute(select(tracks.c.account_key).where(
                (tracks.c.flow == "first_time")
                & (tracks.c.status.in_(OPEN + ("holdout",)) | (tracks.c.first_order_date > fresh))))}
        return keys

    def _episodes(self) -> set[tuple[str, str]]:
        with self.db.engine.begin() as c:
            return {(r[0], r[1]) for r in c.execute(
                select(tracks.c.account_key, tracks.c.episode_key).where(tracks.c.flow == "reorder"))}

    def _last_lines(self, key: str) -> list[dict]:
        """What they bought last time (orders within merge_days of the last one), consumables first."""
        orders = [o for o in (self.world.orders.get(key) or []) if o.date <= self.today]
        last = orders[-1]
        recent = [o for o in orders if (last.date - o.date).days <= self.merge_days]
        by_sku: dict[str, dict] = {}
        for o in recent:
            for ln in [ln for ln in o.lines if ln.cls.status == LIFECYCLE]:
                d = by_sku.setdefault(ln.cls.sku, {
                    "sku": ln.cls.sku, "display": ln.cls.display, "qty": 0, "amount": 0.0, "family": ln.cls.family,
                    "role": ln.cls.role, "applications": list(ln.cls.applications), "url": ln.cls.url})
                d["qty"] += ln.qty or 0
                d["amount"] += ln.amount or 0
        lines = [d for d in by_sku.values() if d["role"] in ("consumable", "prep", "accessory")]
        return sorted(lines, key=lambda d: -d["amount"])


def _primary_family(lines: list[dict]) -> str:
    totals: dict[str, float] = {}
    for ln in lines:
        totals[ln["family"]] = totals.get(ln["family"], 0) + (ln["amount"] or 0)
    return max(totals, key=totals.get) if totals else ""


def _d(v) -> date | None:
    if not v:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))
