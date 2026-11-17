"""Command line entry point: `hunter-retention <command>` (or `python -m hunter_retention.cli`)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from sqlalchemy import delete, insert, select, update

from .alerts import Alerter
from .classify import IGNORED, LIFECYCLE, NON_PRODUCT, Classifier
from .config import ROOT, load_config
from .db import (DB, apollo_fields, approvals, qbo_items, qbo_txn_lines, qbo_txns, review_queue, suppressions,
                 touches, tracks, utcnow)
from .dispatch import HP_FIELDS
from .render import Renderer
from .runner import run_daily

APPROVAL_PHRASE = "SEND REAL EMAILS"


def _db(cfg) -> DB:
    db = DB(cfg.secrets.database_url)
    db.create_all()
    return db


def _qbo(cfg, db):
    from .qbo import QBOClient, TokenStore
    s = cfg.secrets
    return QBOClient(s.qbo_client_id, s.qbo_client_secret, s.qbo_realm_id,
                     TokenStore(db, s.token_encryption_key), cfg.s("qbo.environment", "production"),
                     int(cfg.s("qbo.minor_version", 75)), s.qbo_bootstrap_refresh_token)


def _apollo(cfg):
    from .apollo import ApolloClient
    return ApolloClient(cfg.secrets.apollo_api_key) if cfg.secrets.apollo_api_key else None


def cmd_run(cfg, args) -> int:
    db = _db(cfg)
    today = date.fromisoformat(args.today) if args.today else None
    qbo = None if args.skip_sync else _qbo(cfg, db)
    apollo = _apollo(cfg) if cfg.mode != "dry_run" else None
    stats = run_daily(cfg, db, qbo, apollo, Alerter(cfg.secrets.alert_webhook_url), today=today,
                      skip_sync=args.skip_sync, force_full_sync=args.full_sync)
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_set_qbo_token(cfg, args) -> int:
    from .qbo import TokenStore
    db = _db(cfg)
    TokenStore(db, cfg.secrets.token_encryption_key).save(args.refresh_token)
    print("Stored QuickBooks refresh token. It rotates automatically on every run.")
    return 0


def cmd_setup_apollo(cfg, args) -> int:
    db = _db(cfg)
    api = _apollo(cfg)
    if api is None:
        print("APOLLO_API_KEY not set"); return 1
    existing = {(f.get("label") or f.get("name") or "").strip(): f for f in api.list_custom_fields()}
    for label, ftype in HP_FIELDS.items():
        f = existing.get(label)
        if f is None and args.create:
            try:
                f = api.create_custom_field(label, ftype)
            except Exception as exc:
                if ftype == "textarea":
                    f = api.create_custom_field(label, "string")
                    print(f"  note: textarea type rejected ({exc}); created {label} as string")
                else:
                    raise
            print(f"  created {label}")
        if f is None:
            print(f"  MISSING {label} ({ftype})  -> rerun with --create"); continue
        with db.engine.begin() as c:
            c.execute(delete(apollo_fields).where(apollo_fields.c.label == label))
            c.execute(insert(apollo_fields).values(label=label, field_id=f["id"], type=f.get("type") or ftype))
        print(f"  ok {label} -> {f['id']}")
    print("\nNext: create the four one-step sequences described in docs/SETUP.md and put their ids in "
          "config/settings.yaml under apollo.sequences.")
    return 0


def cmd_approve(cfg, args) -> int:
    if args.confirm != APPROVAL_PHRASE:
        print(f'Refused. Pass --confirm "{APPROVAL_PHRASE}" to approve production email delivery.'); return 1
    db = _db(cfg)
    with db.engine.begin() as c:
        c.execute(delete(approvals).where(approvals.c.key == "production_send"))
        c.execute(insert(approvals).values(key="production_send", approved_by=args.by, approved_at=utcnow(),
                                           note=args.note or ""))
    print(f"Production sending approved by {args.by}. Also requires mode: live and HP_ALLOW_LIVE_SEND=true.")
    return 0


def cmd_revoke(cfg, args) -> int:
    db = _db(cfg)
    with db.engine.begin() as c:
        c.execute(delete(approvals).where(approvals.c.key == "production_send"))
    print("Production sending approval revoked. Live runs will now refuse to send.")
    return 0


def cmd_suppress(cfg, args) -> int:
    db = _db(cfg)
    email = args.email.lower()
    with db.engine.begin() as c:
        c.execute(delete(suppressions).where(suppressions.c.email == email))
        if args.action == "add":
            c.execute(insert(suppressions).values(
                email=email, reason=args.reason or "", created_at=utcnow(),
                source="support_hold" if args.support_hold else "manual"))
    print(f"{args.action} {email}")
    return 0


def cmd_review(cfg, args) -> int:
    db = _db(cfg)
    if args.resolve:
        with db.engine.begin() as c:
            r = c.execute(select(review_queue).where(review_queue.c.id == args.resolve)).mappings().first()
            if not r:
                print("not found"); return 1
            if args.action in ("enroll", "exclude") and r["kind"] in (
                    "domain_match_review", "role_email_only", "missing_email"):
                tr = c.execute(select(tracks).where(tracks.c.account_key == r["ref"])).mappings().first()
                if tr:
                    if args.action == "enroll":
                        vals = {"status": "active", "status_reason": f"review:{r['kind']}"}
                        if args.email:
                            vals["email"] = args.email.lower()
                        c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(**vals, updated_at=utcnow()))
                    else:
                        c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                            status="canceled", status_reason="review_excluded", updated_at=utcnow()))
                        c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                                  .values(status="canceled", reason="review_excluded"))
            c.execute(update(review_queue).where(review_queue.c.id == args.resolve).values(resolved_at=utcnow()))
        print(f"resolved {args.resolve} ({args.action})"); return 0
    with db.engine.begin() as c:
        rows = c.execute(select(review_queue).where(review_queue.c.resolved_at.is_(None))
                         .order_by(review_queue.c.id)).mappings().all()
    for r in rows:
        print(f"#{r['id']:<4} {r['kind']:<32} {r['ref']:<28} {json.dumps(r['details'], default=str)[:160]}")
    if not rows:
        print("Review queue is empty.")
    return 0


def cmd_audit_items(cfg, args) -> int:
    db = _db(cfg)
    clf = Classifier(cfg.products, cfg.lifecycle)
    with db.engine.begin() as c:
        items = c.execute(select(qbo_items)).mappings().all()
        sold = {r[0] for r in c.execute(select(qbo_txn_lines.c.item_id).join(
            qbo_txns, qbo_txns.c.pk == qbo_txn_lines.c.txn_pk).where(qbo_txns.c.status == "active"))}
    bad = 0
    for it in sorted(items, key=lambda r: r["name"] or ""):
        ic = clf.classify_item(it["id"], it["name"])
        flag = ""
        if ic.status not in (LIFECYCLE, NON_PRODUCT, IGNORED):
            flag = "  <-- UNCLASSIFIED" + (" (has sales)" if it["id"] in sold else "")
            bad += 1
        if args.all or flag:
            print(f"{it['id']:>6}  {it['name'] or '':<28} {ic.status:<13} {ic.family or '':<17} {ic.role or '':<11}"
                  f" {','.join(ic.applications):<14}{flag}")
    print(f"\n{bad} unclassified item(s).")
    return 0 if bad == 0 else 2


def cmd_preview(cfg, args) -> int:
    r = Renderer(cfg)
    if args.track:
        db = _db(cfg)
        with db.engine.begin() as c:
            tr = c.execute(select(tracks).where(tracks.c.id == args.track)).mappings().first()
        tr = dict(tr)
    else:
        tr = sample_track(cfg, args.segment, args.app)
    out = r.render(tr, args.touch)
    print(f"Subject: {out.subject}\n\n{out.body}")
    if out.unverified:
        print(f"--- {len(out.unverified)} VERIFY marker(s): live sending blocked until resolved ---")
    return 0


def cmd_content_lint(cfg, args) -> int:
    r = Renderer(cfg)
    total = 0
    combos = [("A", a) for a in ("gold", "nickel", "tin", "silver", "copper", "other", None)] + \
             [("A", "gold+nickel"), ("B", None), ("C", None), ("D", "carbitron"), ("D", "micro_jet"), ("D", "micro_metallizer")]
    for seg, app in combos:
        for t in cfg.lc(f"tracks.{seg}.touches", []) or []:
            out = r.render(sample_track(cfg, seg, app), t["key"])
            for m in sorted(set(out.unverified)):
                print(f"{seg}/{app or '-'}/{t['key']}: {m}")
            total += len(set(out.unverified))
    print(f"\n{total} unverified marker(s). Live sends of affected emails are blocked until they are replaced.")
    return 0


def sample_track(cfg, segment: str, app: str | None) -> dict:
    """Synthetic purchase used for previews and lint."""
    if segment == "A":
        apps = app.split("+") if app else []
        sku_for = {"gold": "PL-1003", "nickel": "PL-1006", "tin": "PL-1012", "silver": "PL-1008",
                   "copper": "PL-1010", "other": "PL-1015"}
        lines = [{"sku": "PL-1000C", "display": "Contact Repair Kit", "qty": 1, "amount": 400, "family": "micro_metallizer",
                  "role": "equipment", "applications": [], "url": "https://hunterproducts.com/products/contact-repair-kit/"}]
        clf = Classifier(cfg.products, cfg.lifecycle)
        for a in apps:
            ic = clf.classify_item(None, sku_for[a])
            lines.append({"sku": ic.sku, "display": ic.display, "qty": 2, "amount": 290, "family": "micro_metallizer",
                          "role": "consumable", "applications": list(ic.applications), "url": ic.url})
        fam, sub = "micro_metallizer", (apps[0] if len(apps) == 1 else ("multi" if apps else "general"))
    elif segment == "B":
        lines = [{"sku": "TS3037-110 VOLT", "display": "Carbitron 300 (110V)", "qty": 1, "amount": 495, "family": "carbitron",
                  "role": "equipment", "applications": [], "url": "https://hunterproducts.com/products/carbitron-300-system/"},
                 {"sku": "TS3038", "display": "Tungsten Carbide Electrode 1/8\" x 2-1/2\"", "qty": 4, "amount": 50,
                  "family": "carbitron", "role": "consumable", "applications": [], "url": None}]
        fam, sub, apps = "carbitron", "carbitron", []
    elif segment == "C":
        lines = [{"sku": "AB2000", "display": "Micro-Jet 200", "qty": 1, "amount": 495, "family": "micro_jet",
                  "role": "equipment", "applications": [], "url": "https://hunterproducts.com/systems/micro-jet-200/"},
                 {"sku": "AB2025", "display": "Aluminum Oxide 5 lb (MGAO)", "qty": 1, "amount": 35, "family": "micro_jet",
                  "role": "consumable", "applications": [], "url": None}]
        fam, sub, apps = "micro_jet", "micro_jet", []
    else:
        fam = app or "carbitron"
        sample = {"carbitron": ("TS3039", "Tungsten Carbide Electrode 1/16\" x 2-1/2\""),
                  "micro_jet": ("AB2003", "Tungsten Carbide Nozzle .020\""),
                  "micro_metallizer": ("PL-1002", "Absorbent/Degreaser Pen")}[fam]
        lines = [{"sku": sample[0], "display": sample[1], "qty": 6, "amount": 75, "family": fam,
                  "role": "prep" if fam == "micro_metallizer" else "consumable", "applications": [], "url": None}]
        sub, apps = fam, []
    return {"id": 0, "account_key": "acct:sample", "segment": segment, "subsegment": sub, "primary_family": fam,
            "applications": apps, "first_name": "Jordan", "last_name": "Sample", "company": "Sample Circuits Inc.",
            "customer_ids": ["0"], "first_order_date": date(2026, 12, 1), "order_summary": {"lines": lines, "orders": []},
            "reorder_status": "none", "email": "jordan@example.com"}


def cmd_report(cfg, args) -> int:
    from .report import write_report
    from .world import load_world
    db = _db(cfg)
    world = load_world(db, cfg, Classifier(cfg.products, cfg.lifecycle))
    today = date.fromisoformat(args.today) if args.today else date.today()
    m = write_report(db, world, today, args.out or ROOT / "reports")
    print(json.dumps({k: m[k] for k in ("new_customers_identified", "emails_sent", "emails_scheduled",
                                        "reorders_after_enrollment", "repeat_revenue_after_outreach")}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="hunter-retention")
    p.add_argument("--config-dir")
    p.add_argument("--content-dir")
    p.add_argument("--mode", choices=["dry_run", "sandbox", "live"], help="override settings.yaml mode")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="daily job: sync, plan, dispatch, report")
    s.add_argument("--today"); s.add_argument("--skip-sync", action="store_true"); s.add_argument("--full-sync", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("init-db"); s.set_defaults(fn=lambda cfg, a: (_db(cfg), print("ok"), 0)[2])
    s = sub.add_parser("set-qbo-token"); s.add_argument("refresh_token"); s.set_defaults(fn=cmd_set_qbo_token)
    s = sub.add_parser("setup-apollo"); s.add_argument("--create", action="store_true"); s.set_defaults(fn=cmd_setup_apollo)
    s = sub.add_parser("approve-production"); s.add_argument("--by", required=True); s.add_argument("--confirm", required=True)
    s.add_argument("--note"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("revoke-production"); s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("suppress"); s.add_argument("action", choices=["add", "remove"]); s.add_argument("email")
    s.add_argument("--reason"); s.add_argument("--support-hold", action="store_true"); s.set_defaults(fn=cmd_suppress)
    s = sub.add_parser("review"); s.add_argument("--resolve", type=int)
    s.add_argument("--action", choices=["enroll", "exclude", "done"], default="done"); s.add_argument("--email")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("audit-items"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_audit_items)
    s = sub.add_parser("preview"); s.add_argument("--segment", default="A"); s.add_argument("--touch", default="e1")
    s.add_argument("--app"); s.add_argument("--track", type=int); s.set_defaults(fn=cmd_preview)
    s = sub.add_parser("content-lint"); s.set_defaults(fn=cmd_content_lint)
    s = sub.add_parser("report"); s.add_argument("--today"); s.add_argument("--out"); s.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    if args.mode:
        import os
        os.environ["HP_MODE"] = args.mode     # CLI flag > HP_MODE env > settings.yaml
    cfg = load_config(args.config_dir, args.content_dir)
    return int(args.fn(cfg, args) or 0)


if __name__ == "__main__":
    sys.exit(main())
