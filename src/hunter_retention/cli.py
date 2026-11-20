"""Command line entry point: `hunter-retention <command>` (or `python -m hunter_retention.cli`)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

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


def _mail(cfg):
    """Read-only Outlook search for the contact lookup. None when the Microsoft app is not set up yet."""
    s = cfg.secrets
    boxes = cfg.s("contacts.mailboxes", []) or []
    if not (s.ms_tenant_id and s.ms_client_id and s.ms_client_secret and boxes):
        return None
    from .mail import GraphMail
    return GraphMail(s.ms_tenant_id, s.ms_client_id, s.ms_client_secret, boxes)


def cmd_run(cfg, args) -> int:
    db = _db(cfg)
    today = date.fromisoformat(args.today) if args.today else None
    qbo = None if args.skip_sync else _qbo(cfg, db)
    apollo = _apollo(cfg) if cfg.mode != "dry_run" else None
    stats = run_daily(cfg, db, qbo, apollo, Alerter(cfg.secrets.alert_webhook_url), today=today,
                      skip_sync=args.skip_sync, force_full_sync=args.full_sync, mail=_mail(cfg))
    print(json.dumps(stats, indent=2, default=str))
    return 0


def cmd_simulate(cfg, args) -> int:
    """Dry run both flows on real data as if they had been switched on N days ago. Writes nothing to the
    production database, sends nothing, makes no Apollo calls."""
    from .runner import local_today
    from .simulate import simulate
    out = Path(args.out or ROOT / "reports")
    summary = simulate(cfg, _db(cfg), local_today(cfg), args.days_back, out, _mail(cfg))
    print("SIMULATION " + json.dumps(summary, default=str))
    return 0


def cmd_contacts(cfg, args) -> int:
    """preview: show who the lookup would pick for recent buyers. Writes nothing, spends no credits.
    list: stored contacts.  set: choose a contact by hand (always wins)."""
    import csv
    from collections import Counter
    from datetime import timedelta

    from .classify import Classifier
    from .contacts import ContactResolver, set_manual_contact
    from .db import contacts as contacts_t
    from .runner import local_today
    from .world import first_purchase, load_world

    if args.action == "mailcheck":
        mail = _mail(cfg)
        if not mail:
            print("Microsoft app not set up: need MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET and contacts.mailboxes")
            return 1
        try:
            rows = mail.check()
        except Exception as exc:
            print(f"Could not sign in to Microsoft: {type(exc).__name__}: {str(exc)[:400]}")
            return 1
        for r in rows:
            print(f"MAILCHECK {r['mailbox']:<28} {r['test']:<32} {r['status']} found={r['found']} {r['code'] or ''} {r['message']}")
        return 0
    db = _db(cfg)
    if args.action == "set":
        if not (args.account and args.email):
            print("usage: contacts set --account acct:123 --email a@b.com [--role user|buyer|owner_operator]")
            return 1
        set_manual_contact(db, args.account, args.email, args.role or "unknown", args.first, args.last)
        print(f"{args.account}: contact set to {args.email} ({args.role or 'unknown'})")
        return 0
    if args.action == "list":
        with db.engine.begin() as c:
            rows = c.execute(select(contacts_t).order_by(contacts_t.c.account_key, contacts_t.c.status)).mappings().all()
        for r in rows:
            print(f"{r['account_key']:<14} {r['status']:<10} {r['role']:<15} {r['score']:>3} {r['source']:<14} {r['email']}")
        print(f"{len(rows)} contact(s)")
        return 0

    today = local_today(cfg)
    clf = Classifier(cfg.products, cfg.lifecycle)
    world = load_world(db, cfg, clf)
    mail = _mail(cfg)
    resolver = ContactResolver(db, cfg, world, clf, today, mail, None)
    since = today - timedelta(days=args.days)
    rows, tally = [], Counter()
    recent = [(key, acct) for key, acct in world.accounts.items()
              if (world.orders.get(key) or []) and world.orders[key][-1].date >= since]
    # accounts with no person in QuickBooks first, then the most recent buyers
    recent.sort(key=lambda ka: (bool(ka[1].emails), -world.orders[ka[0]][-1].date.toordinal()))
    if args.max_accounts:
        recent = recent[: args.max_accounts]
    for key, acct in recent:
        orders = world.orders.get(key) or []
        res = resolver.resolve(acct, first_purchase(world, key, clf, resolver.merge_days), allow_credits=False)
        p = res.primary
        outcome = (f"{p.role} via {p.source}" if p else
                   ("needs Apollo lookup" if res.would_look_up else "no contact found"))
        tally[(res.qbo_had, outcome)] += 1
        best = res.suggested[0] if res.suggested else None
        rows.append({
            "account": key, "company": res.company, "quickbooks_had": res.qbo_had,
            "picked_email": p.email if p else "", "picked_name": p.name if p else "",
            "picked_role": p.role if p else "", "picked_source": p.source if p else "",
            "picked_score": p.score if p else "", "why": "; ".join(p.evidence) if p else "",
            "suggestion": f"{best.name} <{best.email}> ({best.role}, {best.score})" if best else "",
            "apollo_lookup_needed": "; ".join(res.would_look_up),
            "last_order": str(orders[-1].date), "last_order_total": round(orders[-1].total, 2),
        })
    out = Path(args.out or ROOT / "reports" / "contact_preview.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["account"])
        w.writeheader()
        w.writerows(rows)
    print(f"Contact preview for {len(rows)} accounts with an order in the last {args.days} days")
    print(f"Inbox search: {'on' if mail else 'OFF (Microsoft app not set up)'}; Apollo lookups: preview only, 0 credits")
    for (had, outcome), n in sorted(tally.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print(f"  QuickBooks had {had:<10} -> {outcome:<28} {n}")
    if resolver.stats["mail_errors"]:
        print(f"Inbox search errors: {resolver.stats['mail_errors']} (last: {resolver.last_mail_error})")
    print(f"Details: {out}")
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
    print("\nNext: create the seven one-step sequences described in docs/SETUP.md and put their ids in "
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
        manual_contact = None
        with db.engine.begin() as c:
            r = c.execute(select(review_queue).where(review_queue.c.id == args.resolve)).mappings().first()
            if not r:
                print("not found"); return 1
            if args.action in ("enroll", "exclude") and r["kind"] in (
                    "domain_match_review", "role_email_only", "missing_email", "contact_suggestion"):
                tr = c.execute(select(tracks).where(tracks.c.account_key == r["ref"]).order_by(
                    (tracks.c.status != "held"), tracks.c.id.desc())).mappings().first()
                if tr:
                    if args.action == "enroll":
                        vals = {"status": "active", "status_reason": f"review:{r['kind']}"}
                        if args.email:
                            vals["email"] = args.email.lower()
                            manual_contact = (r["ref"], args.email)
                        c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(**vals, updated_at=utcnow()))
                    else:
                        c.execute(update(tracks).where(tracks.c.id == tr["id"]).values(
                            status="canceled", status_reason="review_excluded", updated_at=utcnow()))
                        c.execute(update(touches).where((touches.c.track_id == tr["id"]) & (touches.c.status == "scheduled"))
                                  .values(status="canceled", reason="review_excluded"))
            c.execute(update(review_queue).where(review_queue.c.id == args.resolve).values(resolved_at=utcnow()))
        if manual_contact:
            # remember the choice so the daily lookup never overrides it
            from .contacts import set_manual_contact
            set_manual_contact(db, manual_contact[0], manual_contact[1], getattr(args, "role", None) or "unknown")
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
    if getattr(args, "role", None):
        tr["contact_role"] = args.role
        tr.setdefault("order_summary", {}).setdefault("po_number", "613707")
    out = r.render(tr, args.touch)
    if out.skip:
        print(f"(a {out.variant} does not get {args.touch})")
        return 0
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
                 {"sku": "AB2025", "display": "Aluminum Oxide MGAO-320 (Super Fine), 5 lb", "qty": 1, "amount": 35, "family": "micro_jet",
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


TEST_CORE = [("A", "gold", "e1", "user"), ("A", "gold", "e1", "buyer"), ("A", "gold", "e2", "user"),
             ("A", "gold+nickel", "e3", "user"), ("A", "gold", "e3", "buyer"), ("A", "gold", "e3", "owner_operator"),
             ("A", "gold", "e4", "user"), ("A", "gold", "e4", "buyer"),
             ("B", None, "e1", "user"), ("B", None, "e2", "user"), ("B", None, "e3", "user"), ("B", None, "e4", "buyer"),
             ("C", None, "e1", "user"), ("C", None, "e3", "user"), ("D", "carbitron", "e1", "user"),
             ("R", None, "r1", "user"), ("R", None, "r1", "buyer"), ("R", None, "r2", "user"), ("R", None, "r3", "user")]


def test_versions(cfg, which: str) -> list[tuple[str, str, object]]:
    """(tag, touch_key, rendered) for the test send. smoke = one email, core = each version once, all = every distinct email."""
    r = Renderer(cfg)
    if which == "all":
        segs = [("A", a) for a in ("gold", "gold+nickel", None)] + [("B", None), ("C", None), ("D", "carbitron"),
                                                                   ("D", "micro_jet"), ("D", "micro_metallizer"), ("R", None)]
        combos = [(seg, app, t["key"], role) for seg, app in segs for t in (cfg.lc(f"tracks.{seg}.touches", []) or [])
                  for role in ("user", "buyer", "owner_operator")]
    else:
        combos = TEST_CORE[:1] if which == "smoke" else TEST_CORE
    out, seen = [], set()
    for seg, app, touch, role in combos:
        tr = sample_track(cfg, seg, app)
        tr["contact_role"] = role
        tr.setdefault("order_summary", {}).setdefault("po_number", "613707")
        rendered = r.render(tr, touch)
        if rendered.skip or (rendered.subject, rendered.body) in seen:
            continue
        seen.add((rendered.subject, rendered.body))
        tag = "-".join(x for x in (seg, (app or "").replace("+", "").replace("_", ""), touch, role.split("_")[0]) if x).lower()
        out.append((tag, touch, rendered))
    return out


def cmd_test_send(cfg, args) -> int:
    """Send sample emails to our own inbox through the real Apollo sequences. Never touches a customer:
    every recipient is a plus-address of sandbox.redirect_to, and that address must be on one of our own domains."""
    from .render import body_to_html
    to = (cfg.s("sandbox.redirect_to") or "").lower()
    own = {d.lower() for d in (cfg.s("contacts.own_domains", []) or [])}
    if "@" not in to or to.split("@")[1] not in own:
        print(f"refusing: sandbox.redirect_to ({to!r}) is not on one of our own domains {sorted(own)}"); return 1
    api = _apollo(cfg)
    if api is None:
        print("APOLLO_API_KEY not set"); return 1
    db = _db(cfg)
    with db.engine.begin() as c:
        ids = {r[0]: r[1] for r in c.execute(select(apollo_fields.c.label, apollo_fields.c.field_id))}
    seqs = cfg.s("apollo.sequences", {}) or {}
    missing = [k for k in ("hp_subject", "hp_body") if k not in ids]
    if missing:
        print(f"Apollo fields missing: {missing}. Run setup-apollo --create first."); return 1
    local, domain = to.split("@")
    formats = ["text", "html"] if args.format == "both" else [args.format or cfg.s("apollo.body_format", "text")]
    sent = 0
    for tag, touch, rendered in test_versions(cfg, args.set):
        seq = seqs.get(touch)
        if not seq:
            print(f"  skip {tag}: no sequence id for {touch}"); continue
        for fmt in formats:
            addr = f"{local}+{tag}{'-' + fmt if len(formats) > 1 else ''}@{domain}"
            body = body_to_html(rendered.body) if fmt == "html" else rendered.body
            subject = f"[TEST {tag}{' ' + fmt if len(formats) > 1 else ''}] {rendered.subject}"
            payload = {"first_name": "Test", "last_name": tag, "email": addr, "organization_name": "Hunter Products",
                       "typed_custom_fields": {ids["hp_subject"]: subject, ids["hp_body"]: body}}
            if args.print_only:
                print(f"  would send {touch} -> {addr}: {subject}"); continue
            contact = api.find_contact(addr)
            if contact:
                api.update_contact(contact["id"], payload); cid = contact["id"]
                try:
                    api.remove_from_sequence([seq], cid)
                except Exception:
                    pass
            else:
                cid = api.create_contact(payload)["id"]
            api.add_to_sequence(seq, cid, cfg.s("apollo.send_from_email_account_id"))
            db.log("test_send", f"Test {tag} ({fmt}) -> {addr}", data={"subject": subject, "sequence": seq})
            print(f"  queued {touch} -> {addr}: {subject}")
            sent += 1
    print(f"\n{sent} test email(s) queued in Apollo for {to} (plus-addresses). Nothing went to a customer.")
    return 0


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
    s.add_argument("--role", choices=["user", "buyer", "owner_operator", "unknown"])
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("audit-items"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_audit_items)
    s = sub.add_parser("preview"); s.add_argument("--segment", default="A"); s.add_argument("--touch", default="e1")
    s.add_argument("--app"); s.add_argument("--track", type=int)
    s.add_argument("--role", choices=["user", "buyer", "owner_operator", "unknown"]); s.set_defaults(fn=cmd_preview)
    s = sub.add_parser("content-lint"); s.set_defaults(fn=cmd_content_lint)
    s = sub.add_parser("simulate", help="dry run both flows on real data without touching production")
    s.add_argument("--days-back", type=int, default=75); s.add_argument("--out"); s.set_defaults(fn=cmd_simulate)
    s = sub.add_parser("contacts", help="contact lookup: preview | list | set")
    s.add_argument("action", choices=["preview", "list", "set", "mailcheck"])
    s.add_argument("--days", type=int, default=730); s.add_argument("--out")
    s.add_argument("--max-accounts", type=int, default=0)
    s.add_argument("--account"); s.add_argument("--email"); s.add_argument("--first"); s.add_argument("--last")
    s.add_argument("--role", choices=["user", "buyer", "owner_operator", "unknown"])
    s.set_defaults(fn=cmd_contacts)
    s = sub.add_parser("test-send", help="send sample emails to our own inbox through the Apollo sequences")
    s.add_argument("--set", choices=["smoke", "core", "all"], default="smoke")
    s.add_argument("--format", choices=["text", "html", "both"]); s.add_argument("--print-only", action="store_true")
    s.set_defaults(fn=cmd_test_send)
    s = sub.add_parser("report"); s.add_argument("--today"); s.add_argument("--out"); s.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    if args.mode:
        import os
        os.environ["HP_MODE"] = args.mode     # CLI flag > HP_MODE env > settings.yaml
    cfg = load_config(args.config_dir, args.content_dir)
    return int(args.fn(cfg, args) or 0)


if __name__ == "__main__":
    sys.exit(main())
