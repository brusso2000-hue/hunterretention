"""Daily job: sync QuickBooks -> plan lifecycle -> dispatch due emails -> report."""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import insert, update

from .alerts import Alerter
from .apollo import ApolloAPI
from .classify import Classifier
from .contacts import ContactResolver, MailSearch
from .config import ROOT, Config
from .db import DB, runs, utcnow
from .dispatch import Dispatcher, LiveSendBlocked
from .engine import Planner
from .qbo import QBOSource
from .render import Renderer
from .report import write_report
from .sync import Syncer
from .world import load_world

log = logging.getLogger(__name__)


def local_today(cfg: Config) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


def run_daily(cfg: Config, db: DB, qbo: QBOSource | None, apollo: ApolloAPI | None, alerter: Alerter,
              today: date | None = None, skip_sync: bool = False, force_full_sync: bool = False,
              report_dir: str | Path | None = None, now: datetime | None = None,
              mail: MailSearch | None = None) -> dict:
    today = today or local_today(cfg)
    db.create_all()
    with db.engine.begin() as c:
        run_id = c.execute(insert(runs).values(started_at=utcnow(), mode=cfg.mode, status="running")).inserted_primary_key[0]
    stats: dict = {"mode": cfg.mode, "today": str(today)}
    try:
        classifier = Classifier(cfg.products, cfg.lifecycle)
        syncer = Syncer(db, qbo, str(cfg.history_start_date)) if qbo else None
        if syncer and not skip_sync:
            stats["sync"] = syncer.run(now=now, force_full=force_full_sync)

        world = load_world(db, cfg, classifier)
        # Find the right person at each new customer before planning, so holds for "AP inbox only"
        # clear themselves. Apollo lookups cost credits, so they only run in the modes listed in contacts.credit_modes.
        credit_modes = cfg.s("contacts.credit_modes", ["live"]) or []
        people = apollo if hasattr(apollo, "match_person") and cfg.mode in credit_modes else None
        stats["contacts"] = ContactResolver(db, cfg, world, classifier, today, mail, people, run_id).run()
        stats["plan"] = Planner(db, cfg, classifier, world, today, run_id).plan()

        renderer = Renderer(cfg)
        dispatcher = Dispatcher(db, cfg, renderer, world, today, apollo, alerter, run_id)

        def live_recheck(tr: dict) -> None:
            if not syncer:
                return
            syncer.refresh_customers_live(tr.get("customer_ids") or [], str(tr["first_order_date"]))
            fresh = load_world(db, cfg, classifier)
            Planner(db, cfg, classifier, fresh, today, run_id).plan_one(tr["account_key"])
            dispatcher.world = fresh

        dispatcher.live_recheck = live_recheck
        stats["dispatch"] = dispatcher.run()

        if stats["plan"].get("unclassified"):
            alerter.alert(f"{stats['plan']['unclassified']} first-time order(s) need product classification",
                          "Run `hunter-retention review` and add the items to config/products.yaml")
        if stats["dispatch"].get("failed"):
            alerter.alert(f"{stats['dispatch']['failed']} email enrollment(s) failed permanently")

        stats["report"] = {"written": True}
        write_report(db, world, today, report_dir or ROOT / "reports")
        with db.engine.begin() as c:
            c.execute(update(runs).where(runs.c.id == run_id).values(finished_at=utcnow(), status="ok", stats=stats))
        return stats
    except Exception as exc:
        with db.engine.begin() as c:
            c.execute(update(runs).where(runs.c.id == run_id).values(
                finished_at=utcnow(), status="error", stats=stats, error=str(exc)[:4000]))
        title = "Live send blocked" if isinstance(exc, LiveSendBlocked) else "Daily run failed"
        alerter.alert(title, str(exc))
        raise
