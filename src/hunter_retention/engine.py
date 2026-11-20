"""Lifecycle planner: decides who is a first-time customer, which track they get, when each touch is due,
and which touches a reorder has made unnecessary. Pure decisions + idempotent DB writes."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import insert, select, update

from .classify import Classifier
from .config import Config
from .db import DB, touches, tracks, utcnow
from .identity import Account
from .util import stable_bucket
from .world import (FirstPurchase, Order, World, cohort_replenishment_days, first_purchase, median_or_none,
                    reorders_after)

log = logging.getLogger(__name__)

ACTIVE_TRACK = ("active", "holdout")
OPEN_TOUCH = ("scheduled",)
DONE_TOUCH = ("sent", "suppressed", "expired", "canceled", "failed", "holdout", "recalled")


@dataclass
class ReorderInfo:
    order: Order
    types: set[str]


def classify_reorder(o: Order, family: str, applications: list[str], same_app_required: bool) -> set[str]:
    types = {"any_purchase"}
    fam_lines = o.lines_of(family)
    if fam_lines:
        types.add("same_family")
    cons = o.lines_of(family, ("consumable",))
    if cons:
        if not same_app_required or not applications:
            types.add("consumable_replenishment")
        elif any(set(ln.cls.applications) & set(applications) for ln in cons):
            types.add("consumable_replenishment")
    if any(ln.cls.role == "equipment" for ln in o.lines if ln.cls.status == "lifecycle"):
        types.add("new_equipment")
    return types


REORDER_RANK = ["consumable_replenishment", "same_family", "new_equipment", "any_purchase"]
REORDER_STATUS_NAME = {
    "consumable_replenishment": "replenished",
    "same_family": "same_family",
    "new_equipment": "new_equipment",
    "any_purchase": "other_purchase",
}


class Planner:
    def __init__(self, db: DB, cfg: Config, classifier: Classifier, world: World, today: date, run_id: int | None = None):
        self.db, self.cfg, self.clf, self.world, self.today, self.run_id = db, cfg, classifier, world, today, run_id
        self.merge_days = int(cfg.lc("first_order_merge_days", 2))
        self.stats = {"new_tracks": 0, "held": 0, "holdout": 0, "canceled": 0, "unclassified": 0,
                      "touches_suppressed": 0, "historical_skipped": 0, "rebuilt": 0, "converted": 0}
        self._cohort: dict[str, list[int]] | None = None

    # ------------------------------------------------------------------ entry
    def plan(self) -> dict:
        existing = self._load_tracks()
        for key, acct in self.world.accounts.items():
            try:
                self._plan_account(acct, existing.get(key))
            except Exception as exc:  # one bad record must not stop the run
                log.exception("planning failed for %s", key)
                self.db.log("plan_error", str(exc), "error", self.run_id, account_key=key)
        # tracks whose account vanished entirely (all QBO records deleted/merged away)
        for key, tr in existing.items():
            if key not in self.world.accounts and tr["status"] in ACTIVE_TRACK + ("held",):
                self._cancel(tr, "account_removed_from_quickbooks")
        return self.stats

    def plan_one(self, account_key: str) -> None:
        acct = self.world.accounts.get(account_key)
        with self.db.engine.begin() as c:
            row = c.execute(select(tracks).where((tracks.c.account_key == account_key)
                                                 & (tracks.c.flow == "first_time"))).mappings().first()
        tr = dict(row) if row else None
        if acct is None:
            if tr and tr["status"] in ACTIVE_TRACK + ("held",):
                self._cancel(tr, "account_removed_from_quickbooks")
            return
        self._plan_account(acct, tr)

    def _load_tracks(self) -> dict[str, dict]:
        with self.db.engine.begin() as c:
            return {r["account_key"]: dict(r) for r in
                    c.execute(select(tracks).where(tracks.c.flow == "first_time")).mappings()}

    # ------------------------------------------------------------------ per account
    def _plan_account(self, acct: Account, tr: dict | None) -> None:
        fp = first_purchase(self.world, acct.key, self.clf, self.merge_days)

        if fp is None:
            if tr and tr["status"] in ACTIVE_TRACK + ("held",):
                self._cancel(tr, "no_qualifying_purchase_remaining")   # voided / deleted / refunded
            return

        if fp.date < self.cfg.program_start_date:
            if tr and tr["status"] in ACTIVE_TRACK + ("held",):
                self._cancel(tr, "earlier_purchase_found")             # late import / back-dated order / merge
            else:
                self.stats["historical_skipped"] += 1
            return

        if fp.cls.is_unclassified:
            details = {"account": acct.key, "company": acct.company, "orders": fp.pks,
                       "items": sorted({ln.item_name or "" for ln in fp.cls.unclassified_lines})}
            if self.db.add_review("unclassified_order", acct.key, details):
                self.db.log("unclassified", f"First order for {acct.company} has no mapped products",
                            "warn", self.run_id, account_key=acct.key, data=details)
            self.stats["unclassified"] += 1
            if tr and tr["status"] in ACTIVE_TRACK + ("held",):
                sent = self._sent_count(tr["id"])
                if sent == 0:
                    self._cancel(tr, "first_order_reclassified_unclassified")
            return

        for ln in fp.cls.unclassified_lines:
            self.db.add_review("unclassified_item", ln.cls.sku or (ln.item_name or "?"),
                               {"item_name": ln.item_name, "item_id": ln.item_id, "example_order": fp.pks[0]})

        if tr is None:
            tr = self._create_track(acct, fp)
        else:
            tr = self._revalidate(tr, acct, fp)
        if tr is None:
            return

        if tr["status"] == "held":
            self._recheck_hold(tr, acct)
            return
        if tr["status"] in ACTIVE_TRACK + ("converted", "completed"):
            self._apply_reorders(tr, acct, fp)   # keeps reorder stats current for reporting

    # ------------------------------------------------------------------ creation
    def _create_track(self, acct: Account, fp: FirstPurchase) -> dict | None:
        anchor, basis = self._anchor(fp)
        status, reason = "active", None
        hold = self._contact_problem(acct)
        if hold:
            status, reason = "held", hold
        elif self._domain_conflict(acct, fp):
            policy = self.cfg.s("identity.domain_match_policy", "review")
            if policy == "existing":
                self.stats["historical_skipped"] += 1
                self.db.log("domain_existing", "Same company domain bought before; treated as existing account",
                            run_id=self.run_id, account_key=acct.key)
                return None
            if policy == "review":
                status, reason = "held", "domain_match_review"
        holdout = False
        pct = int(self.cfg.s("analytics.holdout_pct", 0) or 0)
        if status == "active" and pct > 0 and stable_bucket(acct.key) < pct:
            status, reason, holdout = "holdout", "random_holdout_control_group", True

        summary = [{"sku": ln.cls.sku, "display": ln.cls.display, "qty": ln.qty, "amount": ln.amount,
                    "family": ln.cls.family, "role": ln.cls.role, "applications": list(ln.cls.applications),
                    "url": ln.cls.url} for ln in fp.cls.lifecycle_lines]
        vals = dict(
            account_key=acct.key, customer_ids=acct.customer_ids, email=acct.contact_email,
            first_name=acct.first_name, last_name=acct.last_name, company=acct.company,
            contact_role=acct.contact_role, contact_source=acct.contact_source,
            segment=fp.cls.segment, subsegment=fp.cls.subsegment, primary_family=fp.cls.primary_family,
            families=fp.cls.families, applications=fp.cls.applications,
            first_order_pk=fp.primary.pk, first_order_date=fp.date, anchor_date=anchor, anchor_basis=basis,
            order_summary={"orders": fp.pks, "lines": summary,
                           "secondary_segments": fp.cls.secondary_segments,
                           "po_number": fp.primary.po_number, "doc_number": fp.primary.doc_number},
            holdout=holdout, status=status, status_reason=reason, reorder_status="none",
            created_at=utcnow(), updated_at=utcnow(),
        )
        with self.db.engine.begin() as c:
            tid = c.execute(insert(tracks).values(**vals)).inserted_primary_key[0]
        tr = {"id": tid, **vals}
        self._schedule(tr, fp)
        self.stats["new_tracks"] += 1
        if status == "held":
            self.stats["held"] += 1
            self.db.add_review(reason, acct.key, {"company": acct.company, "emails": acct.emails,
                                                  "role_emails": acct.role_emails, "track_id": tid})
        if holdout:
            self.stats["holdout"] += 1
        self.db.log("track_created", f"{acct.company}: segment {fp.cls.segment}/{fp.cls.subsegment} ({status})",
                    run_id=self.run_id, account_key=acct.key, track_id=tid,
                    data={"anchor": str(anchor), "basis": basis, "reason": reason})
        return tr

    def _contact_problem(self, acct: Account) -> str | None:
        if acct.contact_email:
            return None
        if acct.role_emails:
            if self.cfg.s("identity.role_email_policy", "review") == "send":
                acct.emails = list(acct.role_emails)
                return None
            return "role_email_only"
        return "missing_email"

    def _domain_conflict(self, acct: Account, fp: FirstPurchase) -> bool:
        if not acct.domains:
            return False
        for other_key, other in self.world.accounts.items():
            if other_key == acct.key or not (other.domains & acct.domains):
                continue
            orders = self.world.orders.get(other_key) or []
            if orders and orders[0].date < fp.date:
                return True
        return False

    def _anchor(self, fp: FirstPurchase) -> tuple[date, str]:
        a = self.cfg.lc("anchor", {}) or {}
        home = (a.get("home_country") or "US").upper()
        o = fp.primary
        intl = bool(o.ship_country) and o.ship_country.strip().upper() not in {home, "USA", "UNITED STATES", "US"}
        transit = int(a.get("transit_days_international", 10) if intl else a.get("transit_days_domestic", 4))
        if o.ship_date:
            return o.ship_date + timedelta(days=transit), f"ship_date+{transit}d"
        lag = int(a.get("fulfillment_lag_days", 1))
        return o.date + timedelta(days=lag + transit), f"order_date+{lag}d+{transit}d"

    # ------------------------------------------------------------------ scheduling
    def _schedule(self, tr: dict, fp: FirstPurchase) -> None:
        plan = self.cfg.lc(f"tracks.{tr['segment']}.touches", []) or []
        anchor: date = tr["anchor_date"]
        rows = []
        for t in plan:
            if t.get("offset") == "replenishment":
                offset = self._replenishment_days(tr, fp)
            else:
                offset = int(t["offset_days"])
            due = anchor + timedelta(days=offset)
            window_end = due + timedelta(days=int(t.get("window_days", 14)))
            status = "holdout" if tr["status"] == "holdout" else "scheduled"
            rows.append(dict(track_id=tr["id"], touch_key=t["key"], due_date=due, window_end=window_end,
                             status=status, attempt_count=0, updated_at=utcnow()))
        with self.db.engine.begin() as c:
            have = {r[0] for r in c.execute(select(touches.c.touch_key).where(touches.c.track_id == tr["id"]))}
            new = [r for r in rows if r["touch_key"] not in have]
            if new:
                c.execute(insert(touches), new)

    def _replenishment_days(self, tr: dict, fp: FirstPurchase) -> int:
        r = self.cfg.lc("replenishment", {}) or {}
        lo, hi = int(r.get("min_day", 60)), int(r.get("max_day", 90))
        seg = tr["segment"]
        base = float((r.get("default_days") or {}).get(seg, 75))
        if r.get("use_cohort_history", True):
            if self._cohort is None:
                self._cohort = cohort_replenishment_days(self.world, self.clf, self.merge_days)
            m = median_or_none(self._cohort.get(tr["primary_family"], []), int(r.get("min_cohort_size", 8)))
            if m is not None:
                base = m
        qs = r.get("quantity_scaling") or {}
        if qs.get("enabled"):
            ref = float((qs.get("reference_qty") or {}).get(seg, 0) or 0)
            if ref > 0:
                qty = sum(ln.qty for ln in fp.lines
                          if ln.cls.family == tr["primary_family"] and ln.cls.role == "consumable")
                mult = max(1.0, min(float(qs.get("max_multiplier", 1.5)), qty / ref if qty else 1.0))
                base *= mult
        return int(round(max(lo, min(hi, base))))

    # ------------------------------------------------------------------ revalidation
    def _revalidate(self, tr: dict, acct: Account, fp: FirstPurchase) -> dict | None:
        changed_first = tr["first_order_pk"] != fp.primary.pk
        changed_seg = tr["segment"] != fp.cls.segment
        # keep contact details current (QBO edits)
        updates = {}
        if acct.contact_email and acct.contact_email != tr["email"]:
            updates["email"] = acct.contact_email
        if acct.first_name != tr["first_name"]:
            updates["first_name"] = acct.first_name
        if acct.last_name != tr.get("last_name"):
            updates["last_name"] = acct.last_name
        if acct.contact_email and acct.contact_role != (tr.get("contact_role") or "unknown"):
            updates["contact_role"] = acct.contact_role
            updates["contact_source"] = acct.contact_source
        if updates:
            with self.db.engine.begin() as c:
                c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(**updates, updated_at=utcnow()))
            tr.update(updates)
        if not (changed_first or changed_seg):
            return tr
        if tr["status"] in ("canceled", "completed"):
            return tr
        sent = self._sent_count(tr["id"])
        if sent == 0:
            # nothing has gone out yet: rebuild cleanly from the corrected first order
            with self.db.engine.begin() as c:
                c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                          .values(status="canceled", reason="track_rebuilt", updated_at=utcnow()))
                c.execute(touches.delete().where((touches.c.track_id == tr["id"]) & (touches.c.status == "canceled")
                                                 & (touches.c.reason == "track_rebuilt")))
                anchor, basis = self._anchor(fp)
                vals = dict(segment=fp.cls.segment, subsegment=fp.cls.subsegment,
                            primary_family=fp.cls.primary_family, families=fp.cls.families,
                            applications=fp.cls.applications, first_order_pk=fp.primary.pk,
                            first_order_date=fp.date, anchor_date=anchor, anchor_basis=basis, updated_at=utcnow())
                c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(**vals))
            tr.update(vals)
            self._schedule(tr, fp)
            self.stats["rebuilt"] += 1
            self.db.log("track_rebuilt", "First order changed before any email; rebuilt", run_id=self.run_id,
                        account_key=acct.key, track_id=tr["id"])
        else:
            # emails already went out for the old classification: stop rather than send something inconsistent
            self.db.add_review("first_order_changed_after_send", acct.key,
                               {"track_id": tr["id"], "old_first": tr["first_order_pk"], "new_first": fp.primary.pk,
                                "old_segment": tr["segment"], "new_segment": fp.cls.segment})
            self._cancel(tr, "first_order_changed_after_send")
            return None
        return tr

    def _recheck_hold(self, tr: dict, acct: Account) -> None:
        if tr["status_reason"] in ("missing_email", "role_email_only") and self._contact_problem(acct) is None:
            with self.db.engine.begin() as c:
                c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                    status="active", status_reason="contact_fixed", email=acct.contact_email,
                    first_name=acct.first_name, last_name=acct.last_name, contact_role=acct.contact_role,
                    contact_source=acct.contact_source, updated_at=utcnow()))
            self.db.log("hold_released", "Contact email now available", run_id=self.run_id,
                        account_key=acct.key, track_id=tr["id"])

    # ------------------------------------------------------------------ reorders
    def _apply_reorders(self, tr: dict, acct: Account, fp: FirstPurchase) -> None:
        fam = tr["primary_family"]
        same_app = bool(self.cfg.lc("reorder.replenishment_requires_same_application", False))
        infos = [ReorderInfo(o, classify_reorder(o, fam, tr["applications"] or [], same_app))
                 for o in reorders_after(self.world, acct.key, fp) if o.date <= self.today]
        if not infos:
            return
        all_types = set().union(*(i.types for i in infos))
        best = next(t for t in REORDER_RANK if t in all_types)
        first_reorder = infos[0].order
        upd = {}
        if tr.get("reorder_status") != REORDER_STATUS_NAME[best]:
            upd["reorder_status"] = REORDER_STATUS_NAME[best]
        if tr.get("first_reorder_pk") != first_reorder.pk:
            upd.update(first_reorder_pk=first_reorder.pk, first_reorder_date=first_reorder.date)

        plan = {t["key"]: t for t in (self.cfg.lc(f"tracks.{tr['segment']}.touches", []) or [])}
        with self.db.engine.begin() as c:
            open_touches = [dict(r) for r in c.execute(select(touches).where(
                (touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))).mappings()]
            for t in open_touches:
                suppress_on = set((plan.get(t["touch_key"]) or {}).get("suppress_on", []))
                hit = suppress_on & all_types
                if hit:
                    c.execute(update(touches).where(touches.c.id == t["id"]).values(
                        status="suppressed", reason=f"reorder:{sorted(hit)[0]}", updated_at=utcnow()))
                    self.stats["touches_suppressed"] += 1
            remaining = c.execute(select(touches.c.id).where(
                (touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))).first()
            if tr["status"] == "active" and remaining is None:
                upd.update(status="converted", status_reason=f"reorder:{best}")
                self.stats["converted"] += 1
            if upd:
                c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(**upd, updated_at=utcnow()))
        if upd.get("first_reorder_pk"):
            self.db.log("reorder_detected", f"{acct.company}: {sorted(all_types)}", run_id=self.run_id,
                        account_key=acct.key, track_id=tr["id"], data={"order": first_reorder.pk})

    # ------------------------------------------------------------------ helpers
    def _sent_count(self, track_id: int) -> int:
        with self.db.engine.begin() as c:
            return len(c.execute(select(touches.c.id).where(
                (touches.c.track_id == track_id) & (touches.c.status.in_(("sent", "enrolling"))))).all())

    def _cancel(self, tr: dict, reason: str) -> None:
        with self.db.engine.begin() as c:
            c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                status="canceled", status_reason=reason, updated_at=utcnow()))
            c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                      .values(status="canceled", reason=reason, updated_at=utcnow()))
        tr["status"] = "canceled"
        self.stats["canceled"] += 1
        self.db.log("track_canceled", reason, "warn", self.run_id, account_key=tr["account_key"], track_id=tr["id"])
