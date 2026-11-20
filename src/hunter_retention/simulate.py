"""Full dry run of both flows on real data, without touching the production database.

Copies the QuickBooks tables (read-only SELECTs) into a throwaway SQLite file, pretends both flows were
switched on `days_back` days ago, then runs the real daily job once per day up to today in dry_run mode.
Nothing is sent, no Apollo call is made, and the production database is never written to.

The result answers "does it work on my data?": who would have entered each flow, which contact and email
version each would get, what would have been held or flagged, and whether real reorders that happened
during the window stopped the emails.
"""
from __future__ import annotations

import copy
import csv
import os
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import insert, select, update

from .alerts import Alerter
from .config import Config
from .contacts import MailSearch
from .db import (DB, contacts, events, qbo_customers, qbo_items, qbo_txn_lines, qbo_txns, review_queue,
                 suppressions, touches, tracks)
from .runner import run_daily

COPY = (qbo_customers, qbo_items, qbo_txns, qbo_txn_lines, suppressions, contacts)


def simulate(cfg: Config, source: DB, today: date, days_back: int, out_dir: Path,
             mail: MailSearch | None = None) -> dict:
    os.environ["HP_MODE"] = "dry_run"                         # belt and braces: this can never send
    start = today - timedelta(days=days_back)
    sim_cfg = copy.copy(cfg)
    sim_cfg.settings = copy.deepcopy(cfg.settings)
    sim_cfg.settings["mode"] = "dry_run"
    sim_cfg.settings["program_start_date"] = str(start)
    sim_cfg.settings.setdefault("reorder", {})["start_date"] = str(start)

    tmp = Path(tempfile.mkdtemp(prefix="hp-sim-"))
    sim = DB(f"sqlite:///{tmp / 'sim.db'}")
    sim.create_all()
    for table in COPY:
        with source.engine.begin() as c:
            rows = [dict(r) for r in c.execute(select(table)).mappings()]
        for i in range(0, len(rows), 500):
            with sim.engine.begin() as c:
                c.execute(insert(table), rows[i:i + 500])

    errors = 0
    day = start
    while day <= today:
        run_daily(sim_cfg, sim, None, None, Alerter(None), today=day, skip_sync=True, report_dir=tmp / "reports",
                  now=datetime.combine(day, datetime.min.time()) + timedelta(hours=12), mail=mail)
        # A dry run only logs "WOULD SEND" and leaves the email scheduled. Mark those as sent in the copy, so
        # later days behave as they would in production: spacing, frequency caps, follow-ups, completion.
        with sim.engine.begin() as c:
            ids = {r[0] for r in c.execute(select(events.c.touch_id).where(events.c.kind == "dry_run_send"))}
            if ids:
                c.execute(update(touches).where(touches.c.id.in_(ids) & (touches.c.status == "scheduled"))
                          .values(status="sent", sent_on=day))
        day += timedelta(days=1)

    with sim.engine.begin() as c:
        trs = [dict(r) for r in c.execute(select(tracks)).mappings()]
        tch = [dict(r) for r in c.execute(select(touches)).mappings()]
        evs = [dict(r) for r in c.execute(select(events).order_by(events.c.id)).mappings()]
        revs = [dict(r) for r in c.execute(select(review_queue)).mappings()]
    errors = sum(1 for e in evs if e["level"] == "error")
    by_id = {t["id"]: t for t in trs}

    # the first day each email would have gone out (dry run repeats the log line daily until the window ends)
    would: dict[int, dict] = {}
    for e in evs:
        if e["kind"] == "dry_run_send" and e["touch_id"] not in would:
            would[e["touch_id"]] = e
    touch_by_id = {t["id"]: t for t in tch}

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "sim_tracks.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["flow", "company", "account", "started", "status", "why", "product_line", "segment", "to_email",
                    "to_name", "contact_type", "contact_found_via", "last_or_first_order", "usual_gap_days",
                    "days_since_last_order", "emails_that_would_have_sent", "emails_stopped_by_reorder"])
        for t in sorted(trs, key=lambda t: (t["flow"], str(t["created_at"]))):
            mine = [x for x in tch if x["track_id"] == t["id"]]
            osum = t["order_summary"] or {}
            w.writerow([t["flow"], t["company"], t["account_key"], t["anchor_date"], t["status"],
                        t["status_reason"] or "", t["primary_family"], t["segment"], t["email"] or "",
                        " ".join(p for p in (t["first_name"], t["last_name"]) if p), t["contact_role"] or "",
                        t["contact_source"] or "", t["first_order_date"], osum.get("usual_gap_days", ""),
                        osum.get("days_since", ""),
                        " ".join(sorted(x["touch_key"] for x in mine if x["id"] in would)),
                        " ".join(sorted(x["touch_key"] for x in mine if (x["reason"] or "").startswith("reorder")))])
    with (out_dir / "sim_emails.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "flow", "company", "to", "contact_type", "email", "version", "subject", "body"])
        for tid, e in sorted(would.items(), key=lambda kv: kv[1]["id"]):
            t = by_id[touch_by_id[tid]["track_id"]]
            d = e["data"] or {}
            w.writerow([touch_by_id[tid]["sent_on"] or str(e["ts"])[:10], t["flow"], t["company"], t["email"], t["contact_role"],
                        touch_by_id[tid]["touch_key"], d.get("variant"), d.get("subject"), d.get("body")])
    with (out_dir / "sim_flags.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "company", "details"])
        for r in revs:
            d = r["details"] or {}
            w.writerow([r["kind"], d.get("company", ""), {k: v for k, v in d.items() if k != "company"}])

    def flow(name):
        return [t for t in trs if t["flow"] == name]

    summary = {
        "window": f"{start} to {today} ({days_back + 1} daily runs)",
        "errors": errors,
        "error_samples": [f"{e['kind']}: {e['message'][:160]}" for e in evs if e["level"] == "error"][:5],
        "first_time": _flow_summary(flow("first_time"), tch, would),
        "reorder": _flow_summary(flow("reorder"), tch, would),
        "reorder_started_day_one": sum(1 for t in flow("reorder") if t["anchor_date"] == start),
        "reorder_added_later": sum(1 for t in flow("reorder") if t["anchor_date"] != start),
        "flags": dict(Counter(r["kind"] for r in revs)),
        "unverified_copy_emails": sum(1 for e in would.values() if (e["data"] or {}).get("unverified")),
    }
    return summary


def _flow_summary(trs: list[dict], tch: list[dict], would: dict[int, dict]) -> dict:
    ids = {t["id"] for t in trs}
    mine = [x for x in tch if x["track_id"] in ids]
    return {
        "customers": len(trs),
        "by_status": dict(Counter(t["status"] for t in trs)),
        "held_why": dict(Counter(t["status_reason"] for t in trs if t["status"] == "held")),
        "by_product_line": dict(Counter(t["primary_family"] for t in trs)),
        "by_contact_type": dict(Counter(t["contact_role"] or "none" for t in trs)),
        "contact_found_via": dict(Counter(t["contact_source"] or "none" for t in trs if t["email"])),
        "emails_would_have_sent": dict(Counter(x["touch_key"] for x in mine if x["id"] in would)),
        "emails_stopped_by_reorder": sum(1 for x in mine if (x["reason"] or "").startswith("reorder")),
        "emails_skipped_for_buyers": sum(1 for x in mine if (x["reason"] or "").startswith("role:")),
        "emails_expired_unsent": sum(1 for x in mine if x["status"] == "expired"),
    }
