"""Dispatcher: turns due touches into Apollo enrollments, safely.

Per touch, in order:
  expired window -> expire (a late/delayed sync can never send a stale Day-3 email)
  track not active / holdout / held -> skip appropriately
  suppression list, unsubscribe, bounce, reply -> stop
  frequency cap, active in another Apollo sequence, unverified copy -> defer
  live QuickBooks recheck (configured touches, e.g. Day 120) -> may suppress at the last moment
  then: dry_run (log only) | sandbox (send to internal address) | live (enroll real contact)

Idempotency: a touch is marked 'enrolling' and committed BEFORE the Apollo call; a crash leaves it in
'enrolling', and the next run reconciles it against Apollo instead of enrolling twice.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy import func, insert, select, update

from .apollo import not_sent_reason, ApolloAPI, SkippedByApollo, contact_flags, message_flags
from .alerts import Alerter
from .config import Config
from .db import DB, apollo_fields, engagement, suppressions, touches, tracks, utcnow
from .render import Renderer, body_to_html
from .world import World, revenue_12m_by_account

log = logging.getLogger(__name__)


class NoUnusedSequence(Exception):
    """This person has already received every sequence in the pool; Apollo would silently send nothing."""

HP_FIELDS = {
    "quickbooks_customer_id": "string", "first_purchase_date": "date", "first_purchase_product": "string",
    "first_purchase_sku": "string", "product_category": "string", "plating_application": "string",
    "last_purchase_date": "date", "last_purchase_category": "string", "total_orders": "number",
    "customer_lifecycle_stage": "string", "reorder_status": "string", "assigned_sequence": "string",
    "sequence_enrollment_date": "date", "hp_subject": "string", "hp_body": "textarea",
}
SEGMENT_NAMES = {"A": "Micro-Metallizer", "B": "Carbitron 300", "C": "Micro-Jet 200", "D": "Consumables/parts only"}


class LiveSendBlocked(RuntimeError):
    pass


class Dispatcher:
    def __init__(self, db: DB, cfg: Config, renderer: Renderer, world: World, today: date,
                 apollo: ApolloAPI | None, alerter: Alerter, run_id: int | None = None,
                 live_recheck: Callable[[dict], None] | None = None):
        self.db, self.cfg, self.renderer, self.world, self.today = db, cfg, renderer, world, today
        self.apollo, self.alerter, self.run_id = apollo, alerter, run_id
        self.live_recheck = live_recheck
        self.mode = cfg.mode
        self.stats = {"due": 0, "sent": 0, "dry_run": 0, "sandbox": 0, "expired": 0, "suppressed": 0,
                      "deferred": 0, "failed": 0, "recalled": 0, "holdout": 0, "completed_tracks": 0}
        self.engagement_ok = True
        self._fields: dict[str, str] | None = None
        self._content_alerted = False
        self._pool_warned: set[str] = set()

    # ================================================================ gate
    def assert_live_allowed(self) -> None:
        if self.mode != "live":
            return
        problems = []
        if not self.cfg.secrets.allow_live_send:
            problems.append("HP_ALLOW_LIVE_SEND is not 'true'")
        if not self.db.is_approved("production_send"):
            problems.append("production send not approved (run `hunter-retention approve-production`)")
        if self.apollo is None:
            problems.append("Apollo client not configured")
        seqs = self.cfg.s("apollo.sequences", {}) or {}
        missing = [k for k, v in seqs.items() if not v]
        if missing:
            problems.append(f"Apollo sequence ids missing for {missing}")
        if not self.cfg.s("apollo.send_from_email_account_id"):
            problems.append("apollo.send_from_email_account_id missing")
        if problems:
            raise LiveSendBlocked("; ".join(problems))

    # ================================================================ main
    def run(self) -> dict:
        self.assert_live_allowed()
        if self.apollo and self.mode == "live":
            self.recover_enrolling()
            self.sync_engagement()
            self.recall_stale_enrollments()
        due = self._due_touches()
        self.stats["due"] = len(due)
        # The cap is per day, not per run, so the job can safely run several times a day.
        max_sends = int(self.cfg.s("dispatch.max_sends_per_day", self.cfg.s("dispatch.max_sends_per_run", 25)))
        launch = self.cfg.s("dispatch.launch", {}) or {}
        if launch.get("until") and str(self.today) < str(launch["until"]):
            max_sends = min(max_sends, int(launch.get("max_sends_per_day", max_sends)))
        with self.db.engine.begin() as c:
            sends = c.execute(select(func.count()).select_from(touches).where(
                (touches.c.sent_on == self.today) & (touches.c.status == "sent"))).scalar() or 0
        for tr, t in due:
            if sends >= max_sends:
                self._defer(t, "daily_cap")
                continue
            try:
                sent = self._process(tr, t)
                sends += 1 if sent else 0
            except LiveSendBlocked:
                raise
            except Exception as exc:
                log.exception("touch %s failed", t["id"])
                self.db.log("dispatch_error", str(exc), "error", self.run_id, account_key=tr["account_key"],
                            track_id=tr["id"], touch_id=t["id"])
        self._complete_tracks()
        return self.stats

    def _due_touches(self) -> list[tuple[dict, dict]]:
        with self.db.engine.begin() as c:
            rows = c.execute(select(touches, tracks.c.status.label("track_status")).join(
                tracks, tracks.c.id == touches.c.track_id).where(
                (touches.c.status == "scheduled") & (touches.c.due_date <= self.today))
                .order_by(touches.c.due_date, touches.c.id)).mappings().all()
            out = []
            for r in rows:
                tr = dict(c.execute(select(tracks).where(tracks.c.id == r["track_id"])).mappings().first())
                out.append((tr, {k: v for k, v in dict(r).items() if k != "track_status"}))
        return out

    # ================================================================ per touch
    def _process(self, tr: dict, t: dict) -> bool:
        tcfg = self._touch_cfg(tr, t["touch_key"])
        if self.today > t["window_end"]:
            self._set_touch(t, "expired", f"window ended {t['window_end']}")
            self.stats["expired"] += 1
            self.db.log("touch_expired", f"{t['touch_key']} not sent: past window", "warn", self.run_id,
                        account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"])
            return False
        if tr["status"] == "holdout":
            self._set_touch(t, "holdout", "control_group")
            self.stats["holdout"] += 1
            return False
        if tr["status"] == "held":
            self._defer(t, f"track_held:{tr['status_reason']}")
            return False
        if tr["status"] != "active":
            self._set_touch(t, "canceled", f"track_{tr['status']}")
            return False
        email = (tr.get("email") or "").lower()
        if not email:
            self._defer(t, "no_email")
            return False
        # A pen registration is an email the customer asked for (their lot certificate), not marketing.
        # Unsubscribes and bounces still stop it; the marketing-only rules below do not apply.
        requested = tr.get("flow") == "registration"

        sup = self._suppression(email)
        if sup:
            if sup["source"] == "support_hold":
                self._defer(t, "support_hold")
                return False
            self._stop_track(tr, f"suppressed:{sup['source']}")
            self.stats["suppressed"] += 1
            return False

        # House rule: accounts that spend this much a year never get automated emails, in either flow.
        limit = float(self.cfg.s("eligibility.no_automation_min_revenue_12m", 0) or 0)
        if limit and tr["status"] == "active" and not requested:
            rev = self._revenue_12m().get(tr["account_key"], 0.0)
            if rev >= limit:
                self._stop_track(tr, "key_account_no_automation")
                self.stats["suppressed"] += 1
                self.db.add_review("key_account_no_automation", f"{tr['account_key']}|{tr['id']}", {
                    "company": tr["company"], "flow": tr.get("flow"), "revenue_12m": round(rev, 2),
                    "why": f"spends ${rev:,.0f} a year: handle personally, no automated emails"})
                return False

        if self._replied(tr) and self.cfg.s("eligibility.pause_on_reply", True):
            self._stop_track(tr, "customer_replied", final_status="completed")
            return False

        if self.mode == "live" and not self.engagement_ok and self.cfg.s("eligibility.require_engagement_sync", True):
            self._defer(t, "engagement_sync_failed")
            return False

        cap = None if requested else self._frequency_block(tr)
        if cap:
            self._defer(t, cap)
            return False

        if tcfg.get("recheck_live") and self.live_recheck and self.mode != "dry_run":
            self.live_recheck(tr)
            with self.db.engine.begin() as c:
                fresh = c.execute(select(touches.c.status).where(touches.c.id == t["id"])).scalar()
                tr_status = c.execute(select(tracks.c.status).where(tracks.c.id == tr["id"])).scalar()
            if fresh != "scheduled" or tr_status != "active":
                self.db.log("live_recheck_suppressed", "QuickBooks recheck found a reorder; not sending",
                            run_id=self.run_id, account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"])
                self.stats["suppressed"] += 1
                return False

        rendered = self.renderer.render(tr, t["touch_key"])
        if rendered.skip:
            self._set_touch(t, "suppressed", f"role:{rendered.variant}_skips_this_email")
            self.stats["suppressed"] += 1
            self.db.log("role_skip", f"{t['touch_key']} not sent: contact is a {rendered.variant}", run_id=self.run_id,
                        account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"])
            return False
        if self.mode == "live" and not rendered.is_clean:
            self._defer(t, "content_unverified")
            if not self._content_alerted:
                self.alerter.alert("Live send blocked by unverified email copy",
                                   f"segment {tr['segment']} {t['touch_key']}: {rendered.unverified[:3]}")
                self._content_alerted = True
            return False

        if self.mode == "dry_run":
            self.db.log("dry_run_send", f"WOULD SEND {t['touch_key']} to {email}", run_id=self.run_id,
                        account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"],
                        data={"subject": rendered.subject, "body": rendered.body,
                              "unverified": rendered.unverified, "variant": rendered.variant,
                              "contact_role": tr.get("contact_role"), "contact_source": tr.get("contact_source")})
            self.stats["dry_run"] += 1
            return False

        if self.mode == "sandbox":
            return self._send_sandbox(tr, t, rendered)

        return self._send_live(tr, t, rendered, email)

    # ================================================================ sending
    def _all_sequences(self) -> list[str]:
        """Every Apollo sequence we may send through. They are interchangeable: each one sends the
        contact's hp_subject / hp_body once. The per-touch ids come first so reports stay readable."""
        named = [v for v in (self.cfg.s("apollo.sequences", {}) or {}).values() if v]
        pool = [v for v in (self.cfg.s("apollo.sequence_pool", []) or []) if v]
        return list(dict.fromkeys(named + pool))

    def _sequence_id(self, touch_key: str, contact: dict | None = None, email: str | None = None,
                     pool_ok: bool = False) -> str:
        """Apollo sends a contact each sequence only once, ever. So for every email we pick a sequence this
        person has not been through yet: the touch's own sequence first, then any other one. That is what
        lets a customer go through the reorder emails again each time they fall behind."""
        preferred = (self.cfg.s("apollo.sequences", {}) or {}).get(touch_key)
        if not preferred and not (pool_ok and self._all_sequences()):
            raise LiveSendBlocked(f"No Apollo sequence id configured for {touch_key}")
        used = {st.get("emailer_campaign_id") for st in contact_flags(contact)["statuses"]} if contact else set()
        if email:
            with self.db.engine.begin() as c:
                used |= {r[0] for r in c.execute(select(touches.c.apollo_sequence_id).where(
                    (touches.c.sent_to_email == email) & touches.c.apollo_sequence_id.isnot(None)
                    & touches.c.status.in_(("sent", "enrolling", "recalled", "failed"))))}
        candidates = [preferred] + self._all_sequences() if preferred else self._pool_first()
        free = [x for x in dict.fromkeys(candidates) if x not in used]
        if not free:
            raise NoUnusedSequence(f"{email} has been through all {len(self._all_sequences())} sequences")
        low = int(self.cfg.s("apollo.pool_low_warning", 3))
        if len(free) - 1 < low and email not in self._pool_warned:
            self._pool_warned.add(email)
            self.alerter.alert("Sequence pool running low for a customer",
                               f"{email} has {len(free) - 1} unused sequence(s) left after this email. "
                               "Add more one-step sequences to apollo.sequence_pool in config/settings.yaml.")
        return free[0]

    def _pool_first(self) -> list[str]:
        """Emails with no sequence of their own (pen registration) use the spare pool before the named ones."""
        pool = [v for v in (self.cfg.s("apollo.sequence_pool", []) or []) if v]
        return list(dict.fromkeys(pool + self._all_sequences()))

    def _send_sandbox(self, tr: dict, t: dict, rendered) -> bool:
        limit = int(self.cfg.s("sandbox.max_per_run", 5))
        if self.stats["sandbox"] >= limit:
            return False
        to = self.cfg.s("sandbox.redirect_to")
        if not to or self.apollo is None:
            raise LiveSendBlocked("sandbox mode needs sandbox.redirect_to and APOLLO_API_KEY")
        seq = self._sequence_id(t["touch_key"], pool_ok=tr.get("flow") == "registration")
        subject = f"[TEST {tr['segment']}/{t['touch_key']} {tr.get('company') or ''}] {rendered.subject}"
        fields = self._field_values(tr, t, rendered, seq, subject_override=subject)
        contact = self.apollo.find_contact(to)
        payload = {"first_name": "Sandbox", "last_name": "Test", "email": to, "organization_name": "Hunter Products",
                   "typed_custom_fields": fields}
        if contact:
            # An existing record is a real person's contact: change only our two content fields.
            self.apollo.update_contact(contact["id"], {"typed_custom_fields": fields})
            cid = contact["id"]
        else:
            cid = self.apollo.create_contact(payload)["id"]
        self._remove_if_present(contact, seq, cid)
        self.apollo.add_to_sequence(seq, cid, self.cfg.s("apollo.send_from_email_account_id"))
        self.db.log("sandbox_send", f"Sandbox {t['touch_key']} for {tr['company']} -> {to}", run_id=self.run_id,
                    account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"],
                    data={"subject": subject, "body": rendered.body})
        self.stats["sandbox"] += 1
        return True

    def _send_live(self, tr: dict, t: dict, rendered, email: str) -> bool:
        assert self.apollo is not None
        contact = self.apollo.find_contact(email)
        flags = contact_flags(contact)
        if flags["unsubscribed"] or flags["bounced"]:
            src = "apollo_unsubscribe" if flags["unsubscribed"] else "bounce"
            self._add_suppression(email, src, "seen on Apollo contact before send")
            self._stop_track(tr, f"suppressed:{src}")
            self.stats["suppressed"] += 1
            return False
        hp_seqs = set(self._all_sequences())
        other_active = [s for s in flags["active_sequences"] if s not in hp_seqs]
        requested = tr.get("flow") == "registration"
        if other_active and not requested and self.cfg.s("apollo.skip_if_active_in_other_sequences", True):
            self._defer(t, "active_in_other_apollo_sequence")
            return False
        try:
            seq = self._sequence_id(t["touch_key"], contact, email, pool_ok=requested)
        except NoUnusedSequence as exc:
            self._set_touch(t, "failed", f"no_unused_sequence:{exc}"[:250])
            self.stats["failed"] += 1
            self.alerter.alert("Email not sent: sequence pool used up for this customer",
                               f"{tr['company']} {t['touch_key']}: {exc}. Add sequences to apollo.sequence_pool.")
            return False

        # 1) claim the touch before touching Apollo
        with self.db.engine.begin() as c:
            claimed = c.execute(update(touches).where((touches.c.id == t["id"]) & (touches.c.status == "scheduled"))
                                .values(status="enrolling", attempt_count=(t["attempt_count"] or 0) + 1,
                                        apollo_sequence_id=seq, sent_to_email=email,
                                        rendered_subject=rendered.subject, rendered_body=rendered.body,
                                        updated_at=utcnow())).rowcount
        if not claimed:
            return False
        try:
            fields = self._field_values(tr, t, rendered, seq)
            payload = {"first_name": tr.get("first_name") or "", "last_name": tr.get("last_name") or "",
                       "email": email, "organization_name": tr.get("company") or "",
                       "label_names": [self.cfg.s("apollo.contact_label", "HP Lifecycle")],
                       "typed_custom_fields": fields}
            if contact:
                upd = {k: v for k, v in payload.items() if k not in ("email",) and v}
                self.apollo.update_contact(contact["id"], upd)
                cid = contact["id"]
            else:
                cid = self.apollo.create_contact(payload)["id"]
            # No removal here: taking a contact out of a sequence does not let Apollo send it again,
            # it only erases the record that tells us the sequence is used up for this person.
            self.apollo.add_to_sequence(seq, cid, self.cfg.s("apollo.send_from_email_account_id"))
        except SkippedByApollo as exc:
            reason = exc.reason.lower()
            if "unsub" in reason or "bounce" in reason or "opt" in reason:
                self._add_suppression(email, "apollo_unsubscribe", reason)
                self._set_touch(t, "suppressed", f"apollo_skipped:{reason}")
                self._stop_track(tr, f"suppressed:{reason}")
            else:
                self._set_touch(t, "scheduled", f"apollo_skipped:{reason}")
                self.stats["deferred"] += 1
            return False
        except Exception as exc:
            attempts = (t["attempt_count"] or 0) + 1
            max_attempts = int(self.cfg.s("dispatch.max_attempts", 3))
            if attempts >= max_attempts:
                self._set_touch(t, "failed", f"api_error:{str(exc)[:200]}")
                self.stats["failed"] += 1
                self.alerter.alert("Apollo enrollment failed permanently",
                                   f"{tr['company']} {t['touch_key']}: {exc}")
            else:
                self._set_touch(t, "scheduled", f"api_error_retry:{str(exc)[:200]}")
                self.stats["deferred"] += 1
            self.db.log("enroll_error", str(exc), "error", self.run_id, account_key=tr["account_key"],
                        track_id=tr["id"], touch_id=t["id"])
            return False

        with self.db.engine.begin() as c:
            c.execute(update(touches).where(touches.c.id == t["id"]).values(
                status="sent", reason=None, apollo_contact_id=cid, enrolled_at=utcnow(), sent_on=self.today,
                updated_at=utcnow()))
            c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(apollo_contact_id=cid, updated_at=utcnow()))
        self.db.log("enrolled", f"{t['touch_key']} -> {email} ({tr['company']})", run_id=self.run_id,
                    account_key=tr["account_key"], track_id=tr["id"], touch_id=t["id"], data={"sequence": seq})
        self.stats["sent"] += 1
        return True

    def _remove_if_present(self, contact: dict | None, seq: str, cid: str) -> None:
        """Apollo won't re-add a contact already listed in a sequence; clear old membership first."""
        if not contact:
            return
        if any(s.get("emailer_campaign_id") == seq for s in contact_flags(contact)["statuses"]):
            self.apollo.remove_from_sequence([seq], cid, mode="remove")

    def _field_values(self, tr: dict, t: dict, rendered, seq: str, subject_override: str | None = None) -> dict:
        ids = self._field_ids()
        summary = (tr.get("order_summary") or {}).get("lines", [])
        main = sorted(summary, key=lambda ln: -(ln.get("amount") or 0))[0] if summary else {}
        orders = self.world.orders.get(tr["account_key"], [])
        last = orders[-1] if orders else None
        last_cat = None
        if last:
            fams = [ln.cls.family for ln in last.lines if ln.cls.status == "lifecycle"]
            last_cat = fams[0] if fams else None
        apps = (self.cfg.products.get("applications") or {})
        values = {
            "quickbooks_customer_id": ",".join(tr.get("customer_ids") or []),
            "first_purchase_date": str(tr["first_order_date"]),
            "first_purchase_product": main.get("display") or "",
            "first_purchase_sku": main.get("sku") or "",
            "product_category": SEGMENT_NAMES.get(tr["segment"], tr["segment"]),
            "plating_application": ", ".join(apps.get(a, a) for a in (tr.get("applications") or [])),
            "last_purchase_date": str(last.date) if last else "",
            "last_purchase_category": last_cat or "",
            "total_orders": len(orders),
            "customer_lifecycle_stage": (f"pen_registration_{t['touch_key']}" if tr.get("flow") == "registration"
                                         else f"first_purchase_{t['touch_key']}"),
            "reorder_status": tr.get("reorder_status") or "none",
            "assigned_sequence": f"{tr['segment']}-{tr.get('subsegment')}-{t['touch_key']}",
            "sequence_enrollment_date": str(self.today),
            "hp_subject": subject_override or rendered.subject,
            "hp_body": body_to_html(rendered.body) if self.cfg.s("apollo.body_format", "text") == "html" else rendered.body,
        }
        missing = [k for k in ("hp_subject", "hp_body") if k not in ids]
        if missing:
            raise LiveSendBlocked(f"Apollo custom fields missing: {missing}. Run `hunter-retention setup-apollo`.")
        return {ids[k]: v for k, v in values.items() if k in ids and v not in (None, "")}

    def _field_ids(self) -> dict[str, str]:
        if self._fields is None:
            with self.db.engine.begin() as c:
                self._fields = {r[0]: r[1] for r in c.execute(select(apollo_fields.c.label, apollo_fields.c.field_id))}
        return self._fields

    # ================================================================ engagement & recovery
    def sync_engagement(self) -> None:
        lookback = self.today - timedelta(days=int(self.cfg.s("dispatch.engagement_lookback_days", 150)))
        key_by_seq = {v: None for v in self._all_sequences()}
        with self.db.engine.begin() as c:
            rows = c.execute(select(touches, tracks.c.email).join(tracks, tracks.c.id == touches.c.track_id).where(
                (touches.c.status == "sent") & (touches.c.sent_on >= lookback)
            )).mappings().all()
        by_contact: dict[str, list[dict]] = {}
        for r in rows:
            if r["apollo_contact_id"]:
                by_contact.setdefault(r["apollo_contact_id"], []).append(dict(r))
        not_sent: list[str] = []
        try:
            for cid, trs in by_contact.items():
                msgs = self.apollo.message_activity(cid, list(key_by_seq))
                per_seq: dict[str, list[dict]] = {}
                for m in msgs:
                    per_seq.setdefault(m.get("emailer_campaign_id"), []).append(m)
                for t in trs:
                    flags = message_flags(per_seq.get(t["apollo_sequence_id"], []))
                    self._store_engagement(t["id"], flags, per_seq.get(t["apollo_sequence_id"], []))
                    blocked = not_sent_reason(per_seq.get(t["apollo_sequence_id"], []))
                    if blocked:
                        # Apollo took the enrollment and then refused to send. Record it honestly and tell the owner.
                        self._set_touch(t, "failed", f"apollo_not_sent:{blocked}"[:250])
                        self.stats["failed"] += 1
                        not_sent.append(f"{t['email']} ({t['touch_key']}): {blocked}")
                    if flags["unsubscribed"] or flags["bounced"]:
                        self._add_suppression(t["email"], "apollo_unsubscribe" if flags["unsubscribed"] else "bounce",
                                              f"touch {t['id']}")
                contact = self.apollo.find_contact(trs[0]["email"]) if trs[0]["email"] else None
                cf = contact_flags(contact)
                if cf["unsubscribed"]:
                    self._add_suppression(trs[0]["email"], "apollo_unsubscribe", "contact flag")
                if cf["bounced"]:
                    self._add_suppression(trs[0]["email"], "bounce", "contact email_status")
        except Exception as exc:
            self.engagement_ok = False
            self.alerter.alert("Apollo engagement sync failed; sends deferred this run", str(exc))
        if not_sent:
            self.alerter.alert(f"Apollo did not send {len(not_sent)} email(s)", "\n".join(not_sent[:20]))

    def recover_enrolling(self) -> None:
        """Touches left 'enrolling' by a crash: check Apollo, mark sent or release for retry."""
        with self.db.engine.begin() as c:
            stuck = [dict(r) for r in c.execute(select(touches).where(touches.c.status == "enrolling")).mappings()]
        for t in stuck:
            contact = self.apollo.find_contact(t["sent_to_email"]) if t["sent_to_email"] else None
            in_seq = contact and any(s.get("emailer_campaign_id") == t["apollo_sequence_id"]
                                     for s in contact_flags(contact)["statuses"])
            if in_seq:
                self._set_touch(t, "sent", "recovered_after_crash", apollo_contact_id=contact["id"], enrolled_at=utcnow(),
                               sent_on=self.today)
            else:
                self._set_touch(t, "scheduled", "released_after_crash")
            self.db.log("recovered", f"touch {t['id']} -> {'sent' if in_seq else 'scheduled'}", "warn", self.run_id,
                        touch_id=t["id"])

    def recall_stale_enrollments(self) -> None:
        """If a reorder/cancel landed after enrollment but before Apollo actually sent, pull it back."""
        days = max(1, int(self.cfg.s("dispatch.recall_window_hours", 72)) // 24)
        since = self.today - timedelta(days=days)
        with self.db.engine.begin() as c:
            rows = c.execute(select(touches, tracks.c.status.label("tstatus")).join(
                tracks, tracks.c.id == touches.c.track_id).where(
                (touches.c.status == "sent") & (touches.c.sent_on >= since))).mappings().all()
        implied = {"replenished": {"consumable_replenishment", "same_family", "any_purchase"},
                   "same_family": {"same_family", "any_purchase"},
                   "new_equipment": {"new_equipment", "any_purchase"},
                   "other_purchase": {"any_purchase"}}
        for r in rows:
            with self.db.engine.begin() as c:
                tr = dict(c.execute(select(tracks).where(tracks.c.id == r["track_id"])).mappings().first())
            if tr["status"] == "canceled":
                pass                                   # void / refund / earlier purchase / unsubscribe
            elif tr.get("first_reorder_date") and tr["first_reorder_date"] >= r["sent_on"]:
                suppress_on = set(self._touch_cfg(tr, r["touch_key"]).get("suppress_on", []))
                if not (suppress_on & implied.get(tr.get("reorder_status") or "", set())):
                    continue
            else:
                continue
            with self.db.engine.begin() as c:
                eng = c.execute(select(engagement.c.delivered).where(engagement.c.touch_id == r["id"])).scalar()
            if eng:
                continue
            try:
                self.apollo.remove_from_sequence([r["apollo_sequence_id"]], r["apollo_contact_id"], mode="stop")
                self._set_touch(dict(r), "recalled", f"track_{r['tstatus']}_before_delivery")
                self.stats["recalled"] += 1
            except Exception as exc:
                self.alerter.alert("Could not recall enrollment", f"touch {r['id']}: {exc}")

    # ================================================================ helpers
    def _revenue_12m(self) -> dict[str, float]:
        if getattr(self, "_rev_world", None) is not self.world:      # the world is replaced after a live recheck
            self._rev = revenue_12m_by_account(self.world, self.today,
                                               set(self.cfg.s("identity.freemail_domains", [])))
            self._rev_world = self.world
        return self._rev

    def _touch_cfg(self, tr: dict, key: str) -> dict:
        for t in self.cfg.lc(f"tracks.{tr['segment']}.touches", []) or []:
            if t["key"] == key:
                return t
        return {}

    def _replied(self, tr: dict) -> bool:
        with self.db.engine.begin() as c:
            return c.execute(select(engagement.c.touch_id).join(touches, touches.c.id == engagement.c.touch_id).where(
                (touches.c.track_id == tr["id"]) & (engagement.c.replied.is_(True)))).first() is not None

    def _frequency_block(self, tr: dict) -> str | None:
        min_gap = int(self.cfg.s("eligibility.min_days_between_emails", 7))
        max_30 = int(self.cfg.s("eligibility.max_emails_per_30_days", 2))
        with self.db.engine.begin() as c:
            # count across every track sharing this email (duplicate accounts can never double-send)
            sent_dates = [r[0] for r in c.execute(select(touches.c.sent_on).join(tracks, tracks.c.id == touches.c.track_id)
                                                  .where((tracks.c.email == tr["email"]) & (touches.c.status == "sent")))
                          if r[0]]
        if any((self.today - d).days < min_gap for d in sent_dates):
            return "frequency_min_gap"
        if len([d for d in sent_dates if (self.today - d).days < 30]) >= max_30:
            return "frequency_30_day_cap"
        return None

    def _suppression(self, email: str) -> dict | None:
        with self.db.engine.begin() as c:
            r = c.execute(select(suppressions).where(suppressions.c.email == email)).mappings().first()
            return dict(r) if r else None

    def _add_suppression(self, email: str | None, source: str, reason: str) -> None:
        if not email:
            return
        email = email.lower()
        with self.db.engine.begin() as c:
            if not c.execute(select(suppressions.c.email).where(suppressions.c.email == email)).first():
                c.execute(insert(suppressions).values(email=email, source=source, reason=reason, created_at=utcnow()))

    def _store_engagement(self, touch_id: int, flags: dict, raw: list) -> None:
        with self.db.engine.begin() as c:
            if c.execute(select(engagement.c.touch_id).where(engagement.c.touch_id == touch_id)).first():
                c.execute(update(engagement).where(engagement.c.touch_id == touch_id).values(
                    **flags, raw={"messages": raw[:5]}, updated_at=utcnow()))
            else:
                c.execute(insert(engagement).values(touch_id=touch_id, **flags, raw={"messages": raw[:5]},
                                                    updated_at=utcnow()))

    def _set_touch(self, t: dict, status: str, reason: str | None, **extra) -> None:
        with self.db.engine.begin() as c:
            c.execute(update(touches).where(touches.c.id == t["id"]).values(
                status=status, reason=reason, updated_at=utcnow(), **extra))

    def _defer(self, t: dict, reason: str) -> None:
        self._set_touch(t, "scheduled", f"deferred:{reason}")
        self.stats["deferred"] += 1

    def _stop_track(self, tr: dict, reason: str, final_status: str = "canceled") -> None:
        with self.db.engine.begin() as c:
            c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                      .values(status="suppressed", reason=reason, updated_at=utcnow()))
            c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                status=final_status, status_reason=reason, updated_at=utcnow()))
        tr["status"] = final_status
        self.db.log("track_stopped", reason, "warn", self.run_id, account_key=tr["account_key"], track_id=tr["id"])

    def _complete_tracks(self) -> None:
        with self.db.engine.begin() as c:
            active = [r[0] for r in c.execute(select(tracks.c.id).where(tracks.c.status == "active"))]
            for tid in active:
                open_ = c.execute(select(touches.c.id).where(
                    (touches.c.track_id == tid) & (touches.c.status.in_(("scheduled", "enrolling"))))).first()
                if open_ is None:
                    c.execute(update(tracks).where(tracks.c.id == tid).values(
                        status="completed", status_reason="all_touches_done", updated_at=utcnow()))
                    self.stats["completed_tracks"] += 1
