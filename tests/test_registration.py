"""Pen registration: certificate email and use-by reminder. The lot on a certificate is never guessed."""
from __future__ import annotations

from datetime import date

from sqlalchemy import insert, select, update

from hunter_retention.db import registrations, review_queue, suppressions, touches, tracks, utcnow

from conftest import Harness

D = date
ON = {"registration": {"enabled": True}, "apollo": {"sequence_pool": ["seq_p1", "seq_p2", "seq_p3"]},
      "reorder": {"enabled": False}}
LOT = {"code": "HP-2509-G24", "skus": ["PL-1003"], "received": "2025-09-15", "use_by": "2026-09-15",
       "coc_url": "https://hunterproducts.com/coc/HP-2509-G24.pdf"}
TODAY = D(2025, 11, 10)


def make(tmp_path, lots=(LOT,), settings=None, mode="live"):
    from hunter_retention.config import _deep_merge
    tmp_path.mkdir(parents=True, exist_ok=True)
    st = _deep_merge({k: (dict(v) if isinstance(v, dict) else v) for k, v in ON.items()}, settings or {})
    h = Harness(tmp_path, mode=mode, settings=st).strip_verify()
    h.cfg.lots = {"lots": [dict(x) for x in lots]}
    return h


def customer(h, cid="70", email="ap@acme.com", lot="HP-2509-G24", when=D(2025, 10, 1), po="PO-881", lines=None):
    """A customer who bought pens before the lifecycle program started (so no other emails are in play)."""
    h.qbo.customer(cid, f"Acme {cid}", email, given="Pat")
    tid = h.qbo.order(cid, when, lines or [("PL-1003", 4, 145.0)], po=po)
    if lot:
        h.qbo.txns["Invoice"][tid]["Line"][0]["Description"] = f"24K Gold Plating Pen  Lot {lot}"
    return tid


def register(h, email="dana.lee@acme.com", first="Dana", last="Lee", company="Acme", **extra):
    with h.db.engine.begin() as c:
        return c.execute(insert(registrations).values(email=email, first_name=first, last_name=last, company=company,
                                                      status="new", created_at=utcnow(), **extra)).inserted_primary_key[0]


def reg(h, rid):
    with h.db.engine.begin() as c:
        return dict(c.execute(select(registrations).where(registrations.c.id == rid)).mappings().first())


def reg_track(h, rid):
    with h.db.engine.begin() as c:
        tr = c.execute(select(tracks).where(tracks.c.episode_key == f"reg:{rid}")).mappings().first()
        if not tr:
            return None, {}
        return dict(tr), {r["touch_key"]: dict(r) for r in
                          c.execute(select(touches).where(touches.c.track_id == tr["id"])).mappings()}


def reviews(h):
    with h.db.engine.begin() as c:
        return [r[0] for r in c.execute(select(review_queue.c.kind).where(review_queue.c.resolved_at.is_(None)))]


def test_switched_off_by_default_nothing_is_matched_or_sent(tmp_path):
    h = make(tmp_path, settings={"registration": {"enabled": False}})
    customer(h)
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["status"] == "new" and h.apollo.enrollments == []


def test_recorded_lot_sends_the_certificate_to_the_person_who_registered(tmp_path):
    h = make(tmp_path)
    customer(h)
    rid = register(h)
    stats = h.run(TODAY)
    assert stats["registration"]["started"] == 1
    r = reg(h, rid)
    assert r["status"] == "matched" and r["lot_codes"] == ["HP-2509-G24"] and r["account_key"] == "acct:70"
    # goes to the engineer who filled in the form, not the AP address on the QuickBooks record
    assert h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"] and h.apollo.sent_to("ap@acme.com") == []
    tr, t = reg_track(h, rid)
    body = t["c1"]["rendered_body"]
    assert "Hi Dana," in body and "Lot HP-2509-G24: 24K Gold Plating Pen" in body
    assert "Use by: September 15, 2026" in body and "https://hunterproducts.com/coc/HP-2509-G24.pdf" in body
    assert "your order, PO-881." in body and t["c1"]["rendered_subject"] == "Your Certificate of Conformance (PO-881)"
    assert t["x1"]["status"] == "scheduled" and t["x1"]["due_date"] == D(2026, 7, 17)     # 60 days before use-by


def test_no_lot_on_the_order_holds_and_sends_nothing(tmp_path):
    h = make(tmp_path)
    tid = customer(h, lot=None)
    rid = register(h)
    stats = h.run(TODAY)
    assert stats["registration"]["held_new"] == 1
    assert reg(h, rid)["status"] == "held" and reg(h, rid)["status_reason"] == "registration_no_lot_recorded"
    assert h.apollo.enrollments == [] and reg_track(h, rid)[0] is None
    assert reviews(h) == ["registration_no_lot_recorded"]
    # a second run does not raise the alert again
    assert h.run(TODAY)["registration"]["held_new"] == 0
    # once the lot is written on the order, the next run sends and clears the hold
    h.qbo.txns["Invoice"][tid]["Line"][0]["Description"] = "Gold pen lot: HP-2509-G24"
    h.qbo.txns["Invoice"][tid]["MetaData"]["LastUpdatedTime"] = "2025-11-11T09:00:00-08:00"
    h.run(D(2025, 11, 11), force_full_sync=True)
    assert reg(h, rid)["status"] == "matched" and h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]
    assert reviews(h) == []


def test_lot_that_is_not_in_the_registry_is_never_used(tmp_path):
    h = make(tmp_path)
    customer(h, lot="HP-9999-XX")
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["status_reason"] == "registration_unknown_lot" and h.apollo.enrollments == []


def test_lot_without_a_certificate_document_or_use_by_date_holds(tmp_path):
    h = make(tmp_path, lots=[{**LOT, "coc_url": ""}])
    customer(h)
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["status_reason"] == "registration_lot_incomplete" and h.apollo.enrollments == []
    h2 = make(tmp_path / "b", lots=[{**LOT, "use_by": None}])
    customer(h2)
    rid2 = register(h2)
    h2.run(TODAY)
    assert reg(h2, rid2)["status_reason"] == "registration_lot_incomplete"


def test_lot_registered_for_a_different_product_holds(tmp_path):
    h = make(tmp_path, lots=[{**LOT, "skus": ["PL-1006"]}])
    customer(h)
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["status_reason"] == "registration_lot_product_mismatch" and h.apollo.enrollments == []


def test_lot_in_a_quickbooks_custom_field_named_lot(tmp_path):
    h = make(tmp_path)
    tid = customer(h, lot=None)
    h.qbo.txns["Invoice"][tid]["CustomField"].append(
        {"DefinitionId": "2", "Name": "Lot", "Type": "StringType", "StringValue": "hp-2509-g24"})
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["lot_codes"] == ["HP-2509-G24"] and h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_unknown_company_or_personal_address_holds(tmp_path):
    h = make(tmp_path)
    customer(h)
    a, b = register(h, email="dana@othercorp.com"), register(h, email="dana.lee@gmail.com")
    h.run(TODAY)
    assert reg(h, a)["status_reason"] == "registration_no_account"
    assert reg(h, b)["status_reason"] == "registration_no_account" and h.apollo.enrollments == []


def test_company_with_several_customer_records_is_not_guessed(tmp_path):
    h = make(tmp_path)
    customer(h, cid="70", email="ap.east@bigco.com")
    tid2 = customer(h, cid="71", email="ap.west@bigco.com", when=D(2025, 10, 20))
    h.qbo.customers["71"]["CompanyName"] = h.qbo.customers["71"]["DisplayName"] = "BigCo West"
    rid = register(h, email="sam@bigco.com", first="Sam")
    h.run(TODAY)
    assert reg(h, rid)["status_reason"] == "registration_ambiguous_account" and h.apollo.enrollments == []
    # a person says which order it was; the lot still comes from that order
    with h.db.engine.begin() as c:
        c.execute(update(registrations).where(registrations.c.id == rid).values(manual_order_pk=f"Invoice:{tid2}"))
    h.run(D(2025, 11, 11))
    r = reg(h, rid)
    assert r["status"] == "matched" and r["order_pk"] == f"Invoice:{tid2}" and h.apollo.sent_to("sam@bigco.com") == ["seq_p1"]


def test_person_can_confirm_the_lot_by_hand_but_only_a_lot_on_record(tmp_path):
    from hunter_retention.cli import cmd_registrations

    class A:
        action, all, email, first, last, company, order = "resolve", False, None, None, None, None, None
    h = make(tmp_path)
    customer(h, lot=None)
    rid = register(h)
    h.run(TODAY)
    args = A(); args.id, args.lot = rid, "HP-0000-NOPE"
    assert cmd_registrations(h.cfg, args) == 1 and reg(h, rid)["manual_lot"] is None
    args.lot = "hp-2509-g24"
    assert cmd_registrations(h.cfg, args) == 0 and reg(h, rid)["manual_lot"] == "HP-2509-G24"
    h.run(D(2025, 11, 11))
    assert reg(h, rid)["status"] == "matched" and h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_requested_email_is_not_blocked_by_marketing_rules(tmp_path):
    # $10K+ accounts get no automated marketing, and nobody gets two emails within 7 days.
    # Neither rule may stop a certificate the customer asked for.
    h = make(tmp_path, settings={"eligibility": {"no_automation_min_revenue_12m": 500}})
    customer(h)                                       # $580 order, over the test limit
    rid = register(h)
    with h.db.engine.begin() as c:                    # a marketing email went to the same address yesterday
        tid = c.execute(insert(tracks).values(account_key="acct:x", flow="reorder", episode_key="x", email="dana.lee@acme.com",
                                              segment="R", status="completed")).inserted_primary_key[0]
        c.execute(insert(touches).values(track_id=tid, touch_key="r1", status="sent", sent_on=D(2025, 11, 9)))
    h.run(TODAY)
    assert reg(h, rid)["status"] == "matched" and h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_unsubscribed_address_gets_nothing(tmp_path):
    h = make(tmp_path)
    customer(h)
    rid = register(h)
    with h.db.engine.begin() as c:
        c.execute(insert(suppressions).values(email="dana.lee@acme.com", source="apollo_unsubscribe", reason="t",
                                              created_at=utcnow()))
    h.run(TODAY)
    assert h.apollo.enrollments == [] and reg_track(h, rid)[0]["status"] == "canceled"


def test_same_person_registering_the_same_order_twice_is_sent_once(tmp_path):
    h = make(tmp_path)
    customer(h)
    a = register(h)
    h.run(TODAY)
    b = register(h)
    h.run(D(2025, 11, 12))
    assert reg(h, a)["status"] == "matched" and reg(h, b)["status"] == "duplicate"
    assert h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_reminder_goes_out_before_use_by_unless_they_reordered(tmp_path):
    h = make(tmp_path)
    customer(h)
    rid = register(h)
    h.run(TODAY)
    h.run(D(2026, 7, 16))
    assert h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]
    h.run(D(2026, 7, 17))
    assert h.apollo.sent_to("dana.lee@acme.com") == ["seq_p1", "seq_p2"]
    _, t = reg_track(h, rid)
    assert "use-by date on September 15, 2026" in t["x1"]["rendered_subject"]
    assert "Lot HP-2509-G24: 24K Gold Plating Pen, use by September 15, 2026" in t["x1"]["rendered_body"]

    h2 = make(tmp_path / "b")
    customer(h2)
    rid2 = register(h2)
    h2.run(TODAY)
    h2.qbo.order("70", D(2026, 3, 1), [("PL-1003", 4, 145.0)])
    h2.run(D(2026, 7, 17))
    _, t2 = reg_track(h2, rid2)
    assert t2["x1"]["status"] == "suppressed" and h2.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_pen_registered_late_gets_a_later_reminder_or_none(tmp_path):
    h = make(tmp_path, lots=[{**LOT, "use_by": "2025-12-20"}])       # 40 days left
    customer(h)
    rid = register(h)
    h.run(TODAY)
    assert reg_track(h, rid)[1]["x1"]["due_date"] == D(2025, 11, 17)  # a week after the certificate, not same day
    h2 = make(tmp_path / "b", lots=[{**LOT, "use_by": "2025-11-25"}])  # 15 days left: too late to be useful
    customer(h2)
    rid2 = register(h2)
    h2.run(TODAY)
    assert "x1" not in reg_track(h2, rid2)[1] and h2.apollo.sent_to("dana.lee@acme.com") == ["seq_p1"]


def test_dry_run_writes_nothing_to_apollo(tmp_path):
    h = make(tmp_path, mode="dry_run")
    customer(h)
    rid = register(h)
    h.run(TODAY)
    assert reg(h, rid)["status"] == "matched" and h.apollo.enrollments == []
    assert reg_track(h, rid)[1]["c1"]["status"] == "scheduled"


def test_invalid_email_is_rejected(tmp_path):
    h = make(tmp_path)
    customer(h)
    rid = register(h, email="not-an-email")
    h.run(TODAY)
    assert reg(h, rid)["status"] == "rejected"


def test_order_is_named_by_po_number_or_invoice_number(tmp_path):
    h = make(tmp_path)
    customer(h, po="88112")
    rid = register(h)
    h.run(TODAY)
    assert reg_track(h, rid)[1]["c1"]["rendered_subject"] == "Your Certificate of Conformance (PO 88112)"
    h2 = make(tmp_path / "b")
    tid = customer(h2, po=None)
    h2.qbo.txns["Invoice"][tid]["DocNumber"] = "10432"
    rid2 = register(h2)
    h2.run(TODAY)
    assert reg_track(h2, rid2)[1]["c1"]["rendered_subject"] == "Your Certificate of Conformance (invoice 10432)"
