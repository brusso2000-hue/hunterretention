"""Program reporting: JSON metrics + a static HTML dashboard (reports/latest.html).

Attribution is deliberately conservative:
  * "Revenue after outreach"  = repeat-purchase revenue from emailed customers after their first email.
  * "Estimated incremental"   = (emailed repeat rate - holdout repeat rate) x emailed customers x avg repeat
                                 order value. Only shown once the holdout group is large enough.
  Nothing is claimed as caused by email just because it happened after an email.
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

from sqlalchemy import select

from .db import DB, engagement, events, review_queue, touches, tracks
from .world import World

WINDOWS = (30, 60, 90, 120)
SEG = {"A": "Micro-Metallizer", "B": "Carbitron 300", "C": "Micro-Jet 200", "D": "Consumables only"}


def build_metrics(db: DB, world: World, today: date, min_holdout: int = 20) -> dict:
    with db.engine.begin() as c:
        trs = [dict(r) for r in c.execute(select(tracks).where(tracks.c.flow == "first_time")).mappings()]
        tcs = [dict(r) for r in c.execute(select(touches)).mappings()]
        eng = {r["touch_id"]: dict(r) for r in c.execute(select(engagement)).mappings()}
        reviews = [dict(r) for r in c.execute(select(review_queue).where(review_queue.c.resolved_at.is_(None))).mappings()]
        errs = [dict(r) for r in c.execute(select(events).where(events.c.level == "error")
                                           .order_by(events.c.id.desc()).limit(25)).mappings()]

    touches_by_track: dict[int, list[dict]] = defaultdict(list)
    for t in tcs:
        touches_by_track[t["track_id"]].append(t)

    def repeat_orders(tr):
        orders = world.orders.get(tr["account_key"], [])
        first_pks = set((tr.get("order_summary") or {}).get("orders", []))
        return [o for o in orders if o.pk not in first_pks and o.date >= tr["first_order_date"]]

    enrolled = [t for t in trs if not t["holdout"] and t["status"] not in ("held",)]
    holdout = [t for t in trs if t["holdout"]]

    def rate(group, n_days):
        matured = [t for t in group if t["first_order_date"] + timedelta(days=n_days) <= today]
        if not matured:
            return None, 0
        hit = sum(1 for t in matured if any((o.date - t["first_order_date"]).days <= n_days for o in repeat_orders(t)))
        return hit / len(matured), len(matured)

    repeat_rates = {}
    for n in WINDOWS:
        r_e, n_e = rate(enrolled, n)
        r_h, n_h = rate(holdout, n)
        repeat_rates[n] = {"emailed": r_e, "emailed_n": n_e, "holdout": r_h, "holdout_n": n_h}

    # revenue after first email
    rev_after, repeat_values, days_to_second = 0.0, [], []
    rev_by_seg = Counter()
    for t in enrolled:
        sent = sorted([x["sent_on"] for x in touches_by_track[t["id"]] if x["status"] == "sent" and x["sent_on"]])
        ros = repeat_orders(t)
        if ros:
            days_to_second.append((ros[0].date - t["first_order_date"]).days)
        if not sent:
            continue
        for o in ros:
            if o.date >= sent[0]:
                rev_after += o.product_amount
                rev_by_seg[t["segment"]] += o.product_amount
                repeat_values.append(o.product_amount)

    incremental = None
    r120 = repeat_rates[120]
    if r120["holdout_n"] >= min_holdout and r120["emailed"] is not None and repeat_values:
        lift = r120["emailed"] - r120["holdout"]
        incremental = {"lift_pts": round(lift * 100, 1),
                       "estimated_revenue": round(lift * r120["emailed_n"] * (sum(repeat_values) / len(repeat_values)), 2)}

    touch_status = Counter((t["touch_key"], t["status"]) for t in tcs)
    sent_ids = [t["id"] for t in tcs if t["status"] == "sent"]
    delivered = [eng[i] for i in sent_ids if i in eng and eng[i]["delivered"]]
    approaching = [t for t in tcs if t["touch_key"] == "e3" and t["status"] == "scheduled"
                   and today <= t["due_date"] <= today + timedelta(days=14)]
    no_reorder_120 = [t for t in enrolled if t["first_order_date"] + timedelta(days=120) <= today
                      and not t.get("first_reorder_date")]
    month = Counter(t["first_order_date"].strftime("%Y-%m") for t in trs)

    return {
        "as_of": str(today),
        "new_customers_identified": len(trs),
        "new_customers_by_month": dict(sorted(month.items())),
        "tracks_by_segment": {SEG.get(k, k): v for k, v in Counter(t["segment"] for t in trs).items()},
        "tracks_by_status": dict(Counter(t["status"] for t in trs)),
        "touches": {f"{k}:{s}": v for (k, s), v in sorted(touch_status.items())},
        "emails_scheduled": sum(1 for t in tcs if t["status"] == "scheduled"),
        "emails_sent": len(sent_ids),
        "open_rate": (sum(1 for e in delivered if e["opened"]) / len(delivered)) if delivered else None,
        "reply_rate": (sum(1 for e in delivered if e["replied"]) / len(delivered)) if delivered else None,
        "reorders_after_enrollment": sum(1 for t in enrolled if t.get("first_reorder_date")),
        "repeat_revenue_after_outreach": round(rev_after, 2),
        "repeat_revenue_by_segment": {SEG.get(k, k): round(v, 2) for k, v in rev_by_seg.items()},
        "repeat_purchase_rates": repeat_rates,
        "median_days_first_to_second": median(days_to_second) if days_to_second else None,
        "estimated_incremental": incremental,
        "approaching_replenishment": [{"track_id": t["track_id"], "due": str(t["due_date"])} for t in approaching],
        "no_reorder_after_120_days": len(no_reorder_120),
        "open_review_items": [{"kind": r["kind"], "ref": r["ref"], "details": r["details"]} for r in reviews],
        "recent_errors": [{"ts": str(e["ts"]), "kind": e["kind"], "message": e["message"]} for e in errs],
        "failed_touches": sum(1 for t in tcs if t["status"] == "failed"),
    }


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_html(m: dict) -> str:
    e = html.escape
    rr_rows = "".join(
        f"<tr><td>{n} days</td><td>{_pct(v['emailed'])} <small>(n={v['emailed_n']})</small></td>"
        f"<td>{_pct(v['holdout'])} <small>(n={v['holdout_n']})</small></td></tr>"
        for n, v in m["repeat_purchase_rates"].items())
    reviews = "".join(f"<tr><td>{e(r['kind'])}</td><td>{e(str(r['ref']))}</td><td><code>{e(json.dumps(r['details'], default=str))[:300]}</code></td></tr>"
                      for r in m["open_review_items"]) or "<tr><td colspan=3>None</td></tr>"
    errs = "".join(f"<tr><td>{e(x['ts'])}</td><td>{e(x['kind'])}</td><td>{e(x['message'][:300])}</td></tr>"
                   for x in m["recent_errors"]) or "<tr><td colspan=3>None</td></tr>"
    touches = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in m["touches"].items())
    inc = m["estimated_incremental"]
    inc_txt = (f"{inc['lift_pts']} pt lift vs holdout, about ${inc['estimated_revenue']:,.0f}" if inc
               else "Not enough holdout customers yet to estimate")
    kpis = [
        ("New customers", m["new_customers_identified"]), ("Emails sent", m["emails_sent"]),
        ("Scheduled", m["emails_scheduled"]), ("Reorders after enrollment", m["reorders_after_enrollment"]),
        ("Repeat revenue after outreach", f"${m['repeat_revenue_after_outreach']:,.0f}"),
        ("No reorder at 120d", m["no_reorder_after_120_days"]),
        ("Open review items", len(m["open_review_items"])), ("Failed sends", m["failed_touches"]),
    ]
    tiles = "".join(f"<div class=tile><div class=v>{e(str(v))}</div><div class=l>{e(k)}</div></div>" for k, v in kpis)
    seg = "".join(f"<tr><td>{e(k)}</td><td>{v}</td><td>${m['repeat_revenue_by_segment'].get(k, 0):,.0f}</td></tr>"
                  for k, v in m["tracks_by_segment"].items())
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8><title>Hunter retention report {e(m['as_of'])}</title>
<style>body{{font:14px/1.45 system-ui,Segoe UI,Arial;margin:24px;color:#1d232a;background:#f6f7f8}}
h1{{font-size:20px}}h2{{font-size:15px;margin-top:28px}}.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.tile{{background:#fff;border:1px solid #dde1e5;border-radius:6px;padding:12px}}.v{{font-size:22px;font-weight:600}}
.l{{color:#5b6570;font-size:12px}}table{{border-collapse:collapse;background:#fff;width:100%}}
td,th{{border:1px solid #dde1e5;padding:6px 8px;text-align:left;vertical-align:top}}small{{color:#5b6570}}
.note{{color:#5b6570;font-size:12px}}</style></head><body>
<h1>Hunter Products first-time customer program, as of {e(m['as_of'])}</h1>
<div class=tiles>{tiles}</div>
<h2>Repeat purchase rate (customers old enough for each window)</h2>
<table><tr><th>Window</th><th>Emailed</th><th>Holdout (no email)</th></tr>{rr_rows}</table>
<p class=note>Estimated incremental effect: {e(inc_txt)}. Median days first to second order: {m['median_days_first_to_second']}.
Open rate {_pct(m['open_rate'])}, reply rate {_pct(m['reply_rate'])} (Apollo tracking; opens are unreliable with image blocking).</p>
<h2>By product track</h2><table><tr><th>Track</th><th>Customers</th><th>Repeat revenue after first email</th></tr>{seg}</table>
<h2>Email touches</h2><table><tr><th>Touch:status</th><th>Count</th></tr>{touches}</table>
<h2>Needs review</h2><table><tr><th>Kind</th><th>Ref</th><th>Details</th></tr>{reviews}</table>
<h2>Recent errors</h2><table><tr><th>When</th><th>Kind</th><th>Message</th></tr>{errs}</table>
</body></html>"""


def write_report(db: DB, world: World, today: date, out_dir: str | Path) -> dict:
    m = build_metrics(db, world, today)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(m, indent=2, default=str))
    (out / "latest.html").write_text(render_html(m))
    return m
