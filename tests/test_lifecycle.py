"""End-to-end scenario tests (Phase 5). Every test drives the real daily job day by day against fakes."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import insert, select, update

from hunter_retention.db import events, review_queue, suppressions, touches, utcnow
from hunter_retention.dispatch import LiveSendBlocked

from conftest import Harness

D = date
GOLD_KIT = [("PL-1000C", 1, 400.0), ("PL-1003", 2, 145.0)]


def acct(cid):
    return f"acct:{cid}"


def new_gold_customer(h, cid="10", email="jane@acmecircuits.com", day=D(2026, 1, 1)):
    h.qbo.customer(cid, "Acme Circuits", email, given="Jane")
    return h.qbo.order(cid, day, GOLD_KIT, ship_date=day)


# ------------------------------------------------------------------ core lifecycle
def test_first_time_gold_kit_full_lifecycle_never_reorders(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 5, 10))
    t = h.touches(acct("10"))
    assert {k: v["status"] for k, v in t.items()} == {"e1": "sent", "e2": "sent", "e3": "sent", "e4": "sent"}
    # anchor = ship date + 4 transit = Jan 5
    assert t["e1"]["due_date"] == D(2026, 1, 9)
    assert t["e2"]["due_date"] == D(2026, 2, 4)
    assert D(2026, 3, 6) <= t["e3"]["due_date"] <= D(2026, 4, 5)        # 60-90 day clamp
    assert t["e4"]["due_date"] == D(2026, 5, 5)
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1", "seq_e2", "seq_e3", "seq_e4"]
    tr = h.track(acct("10"))
    assert tr["segment"] == "A" and tr["subsegment"] == "gold" and tr["status"] == "completed"
    # content was personalized and written to Apollo fields
    c = h.apollo.find_contact("jane@acmecircuits.com")
    assert "Contact Repair Kit" in c["typed_custom_fields"]["f_hp_body"] or "Checking in" in c["typed_custom_fields"]["f_hp_subject"]
    assert c["typed_custom_fields"]["f_quickbooks_customer_id"] == "10"


def test_spec_example_reorder_on_feb_15_cancels_refill_and_day_120(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 2, 14))
    h.qbo.order("10", D(2026, 2, 15), [("PL-1003", 3, 145.0)])
    h.run_range(D(2026, 2, 15), D(2026, 6, 1))
    t = h.touches(acct("10"))
    assert t["e1"]["status"] == "sent" and t["e2"]["status"] == "sent"
    assert t["e3"]["status"] == "suppressed" and t["e3"]["reason"].startswith("reorder:")
    assert t["e4"]["status"] == "suppressed"
    tr = h.track(acct("10"))
    assert tr["status"] == "converted" and tr["reorder_status"] == "replenished"
    assert tr["first_reorder_date"] == D(2026, 2, 15)
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1", "seq_e2"]


def test_unrelated_purchase_keeps_refill_but_cancels_day_120(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 2, 14))
    h.qbo.order("10", D(2026, 2, 15), [("TS3038", 4, 12.5)])          # Carbitron electrode, not pens
    h.run_range(D(2026, 2, 15), D(2026, 6, 1))
    t = h.touches(acct("10"))
    assert t["e3"]["status"] == "sent"
    assert t["e4"]["status"] == "suppressed"
    assert h.track(acct("10"))["reorder_status"] == "other_purchase"


def test_idempotent_multiple_runs_same_day(h):
    new_gold_customer(h)
    for _ in range(3):
        h.run(D(2026, 1, 9))
    h.run(D(2026, 1, 9))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]
    assert len(h.touches()) == 4


# ------------------------------------------------------------------ first-time detection
def test_existing_customer_with_history_is_excluded(h):
    h.qbo.customer("20", "Old Shop", "buyer@oldshop.com", given="Sam")
    h.qbo.order("20", D(2024, 5, 1), [("PL-1006", 2, 70.0)])
    h.qbo.order("20", D(2026, 1, 3), GOLD_KIT)
    h.run_range(D(2026, 1, 3), D(2026, 2, 10))
    assert h.track(acct("20")) is None
    assert h.apollo.enrollments == []


def test_duplicate_qbo_records_same_email_share_history(h):
    h.qbo.customer("30", "Beta Mfg", "pat@betamfg.com", given="Pat")
    h.qbo.order("30", D(2023, 3, 1), [("PL-1003", 1, 145.0)])
    h.qbo.customer("31", "Beta Manufacturing (new record)", "pat@betamfg.com", given="Pat")
    h.qbo.order("31", D(2026, 1, 5), GOLD_KIT)
    h.run_range(D(2026, 1, 5), D(2026, 2, 10))
    assert h.track(acct("30")) is None and h.track(acct("31")) is None
    assert h.apollo.enrollments == []


def test_duplicate_records_same_company_and_domain_merge(h):
    h.qbo.customer("32", "Gamma Plating Inc.", "lee@gammaplating.com", given="Lee")
    h.qbo.order("32", D(2022, 3, 1), [("PL-1003", 1, 145.0)])
    h.qbo.customer("33", "Gamma Plating", "kim@gammaplating.com", given="Kim")
    h.qbo.order("33", D(2026, 1, 5), GOLD_KIT)
    h.run_range(D(2026, 1, 5), D(2026, 1, 20))
    assert h.apollo.enrollments == []


def test_sub_customer_job_rolls_up_to_parent_history(h):
    h.qbo.customer("40", "TTM Corp", "buyer@ttm.com", given="Alex")
    h.qbo.order("40", D(2025, 1, 1), [("PL-1003", 10, 145.0)])
    h.qbo.customer("41", "TTM Corp:Plant 7", "eng7@ttm.com", given="Chris", parent="40")
    h.qbo.order("41", D(2026, 1, 2), [("PL-1003", 2, 145.0)])
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    assert h.apollo.enrollments == []


def test_new_contact_at_existing_company_domain_held_for_review(h):
    h.qbo.customer("50", "Delta Electronics", "ap@deltaelec.com")
    h.qbo.order("50", D(2024, 1, 1), [("PL-1003", 2, 145.0)])
    h.qbo.customer("51", "Delta East Facility", "maria@deltaelec.com", given="Maria")
    h.qbo.order("51", D(2026, 1, 2), GOLD_KIT, ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    tr = h.track(acct("51"))
    assert tr["status"] == "held" and tr["status_reason"] == "domain_match_review"
    assert h.apollo.enrollments == []


def test_backdated_invoice_imported_later_cancels_track(h):
    new_gold_customer(h)
    h.run(D(2026, 1, 1))
    assert h.track(acct("10"))["status"] == "active"
    h.qbo.order("10", D(2025, 6, 1), [("PL-1006", 1, 70.0)])           # late import of an old order
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    tr = h.track(acct("10"))
    assert tr["status"] == "canceled" and tr["status_reason"] == "earlier_purchase_found"
    assert h.apollo.enrollments == []


def test_voided_first_order_never_enrolls(h):
    tid = new_gold_customer(h)
    h.qbo.void(tid)
    h.run_range(D(2026, 1, 1), D(2026, 1, 20))
    assert h.track(acct("10")) is None


def test_void_after_track_created_cancels(h):
    tid = new_gold_customer(h)
    h.run(D(2026, 1, 1))
    h.qbo.void(tid)
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    assert h.track(acct("10"))["status"] == "canceled"
    assert h.apollo.enrollments == []


def test_deleted_first_order_cancels(h):
    tid = new_gold_customer(h)
    h.run(D(2026, 1, 1))
    h.qbo.delete(tid)
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    assert h.track(acct("10"))["status"] == "canceled"


def test_full_refund_cancels(h):
    new_gold_customer(h)
    h.run(D(2026, 1, 1))
    h.qbo.order("10", D(2026, 1, 3), [("PL-1000C", 1, 400.0), ("PL-1003", 2, 145.0)], kind="CreditMemo")
    h.run_range(D(2026, 1, 3), D(2026, 1, 20))
    assert h.track(acct("10"))["status"] == "canceled"
    assert h.apollo.enrollments == []


def test_zero_dollar_sample_is_not_a_purchase(h):
    h.qbo.customer("60", "Sample Co", "eng@sampleco.com", given="Robin")
    h.qbo.order("60", D(2026, 1, 2), [("PL-1003", 1, 0.0)])
    h.run_range(D(2026, 1, 2), D(2026, 1, 20))
    assert h.track(acct("60")) is None


# ------------------------------------------------------------------ segmentation
def test_mixed_carbitron_and_pens_goes_to_carbitron_track(h):
    h.qbo.customer("70", "MedTool Repair", "ops@medtoolrepair.com", given="Dana")
    h.qbo.order("70", D(2026, 1, 2), [("TS3037-110 Volt", 1, 495.0), ("PL-1003", 1, 145.0)], ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 12))
    tr = h.track(acct("70"))
    assert tr["segment"] == "B" and "A" in tr["order_summary"]["secondary_segments"]
    c = h.apollo.find_contact("ops@medtoolrepair.com")
    assert "Carbitron" in c["typed_custom_fields"]["f_hp_subject"]
    assert "Micro-Metallizer" not in c["typed_custom_fields"]["f_hp_body"]


def test_pen_customer_never_gets_carbitron_copy(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 5, 10))
    with h.db.engine.begin() as c:
        bodies = [r[0] for r in c.execute(select(touches.c.rendered_body))]
    assert bodies and not any("Carbitron" in b for b in bodies)


def test_multi_metal_order_gets_multi_subsegment(h):
    h.qbo.customer("71", "Epsilon PCB", "tech@epsilonpcb.com", given="Ari")
    h.qbo.order("71", D(2026, 1, 2), [("PL-1003", 2, 145.0), ("PL-1006", 2, 70.0)], ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 2, 6))
    tr = h.track(acct("71"))
    assert tr["subsegment"] == "multi" and set(tr["applications"]) == {"gold", "nickel"}
    with h.db.engine.begin() as c:
        e2 = c.execute(select(touches.c.rendered_body).where(touches.c.touch_key == "e2")).scalar()
    assert "PL-1003" in e2 and "PL-1006" in e2          # the one-month email lists both pens they bought


def test_consumables_only_buyer_goes_to_segment_d(h):
    h.qbo.customer("72", "Zeta Tools", "buyer@zetatools.com", given="Lou")
    h.qbo.order("72", D(2026, 1, 2), [("TS3039", 6, 12.5)], ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 12))
    tr = h.track(acct("72"))
    assert tr["segment"] == "D" and tr["subsegment"] == "carbitron"
    assert set(h.touches(acct("72"))) == {"e1", "e3", "e4"}


def test_unclassified_product_flags_and_does_not_enroll_until_mapped(h):
    h.qbo.customer("80", "Eta Labs", "sci@etalabs.com", given="Mo")
    h.qbo.order("80", D(2026, 1, 2), [("NEW-PEN-9000", 1, 199.0)], ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 4))
    assert h.track(acct("80")) is None
    with h.db.engine.begin() as c:
        kinds = [r[0] for r in c.execute(select(review_queue.c.kind))]
    assert "unclassified_order" in kinds
    assert any("classification" in a for a in h.alerter.sent)
    # Brandon maps the new SKU in products.yaml
    h.cfg.products["items"].append({"name": "NEW-PEN-9000", "family": "micro_metallizer", "role": "consumable",
                                    "application": "gold", "display": "New Gold Pen"})
    h.run_range(D(2026, 1, 5), D(2026, 1, 12))
    assert h.track(acct("80"))["segment"] == "A"
    assert h.apollo.sent_to("sci@etalabs.com") == ["seq_e1"]


def test_unclassified_line_alongside_known_products_is_flagged_not_blocking(h):
    h.qbo.customer("81", "Theta", "ops@theta.com", given="Nat")
    h.qbo.order("81", D(2026, 1, 2), GOLD_KIT + [("MYSTERY-1", 1, 50.0)], ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 12))
    assert h.track(acct("81"))["segment"] == "A"
    with h.db.engine.begin() as c:
        refs = [r[0] for r in c.execute(select(review_queue.c.ref).where(review_queue.c.kind == "unclassified_item"))]
    assert "MYSTERY-1" in refs


# ------------------------------------------------------------------ contact quality
def test_missing_email_held_then_released_when_added(h):
    h.qbo.customer("90", "Iota Inc", None)
    h.qbo.order("90", D(2026, 1, 2), GOLD_KIT, ship_date=D(2026, 1, 2))
    h.run_range(D(2026, 1, 2), D(2026, 1, 7))
    assert h.track(acct("90"))["status"] == "held"
    h.qbo.customers["90"]["PrimaryEmailAddr"] = {"Address": "kai@iota.com"}
    h.qbo.customers["90"]["GivenName"] = "Kai"
    h.run_range(D(2026, 1, 8), D(2026, 1, 12))
    assert h.track(acct("90"))["status"] == "active"
    assert h.apollo.sent_to("kai@iota.com") == ["seq_e1"]


def test_accounts_payable_only_address_is_held(h):
    h.qbo.customer("91", "Kappa Corp", "accountspayable@kappa.com")
    h.qbo.order("91", D(2026, 1, 2), GOLD_KIT)
    h.run_range(D(2026, 1, 2), D(2026, 1, 12))
    assert h.track(acct("91"))["status_reason"] == "role_email_only"
    assert h.apollo.enrollments == []


# ------------------------------------------------------------------ suppression
def test_unsubscribe_in_apollo_stops_everything(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 1, 10))
    c = h.apollo.find_contact("jane@acmecircuits.com")
    c["email_unsubscribed"] = True
    h.run_range(D(2026, 1, 11), D(2026, 6, 1))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]
    with h.db.engine.begin() as cx:
        assert cx.execute(select(suppressions.c.source)).scalar() == "apollo_unsubscribe"
    assert h.track(acct("10"))["status"] == "canceled"


def test_reply_hands_off_to_human(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 1, 10))
    cid = h.apollo.find_contact("jane@acmecircuits.com")["id"]
    h.apollo.messages[cid] = [{"emailer_campaign_id": "seq_e1", "status": "completed", "replied": True}]
    h.run_range(D(2026, 1, 11), D(2026, 6, 1))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]
    assert h.track(acct("10"))["status_reason"] == "customer_replied"


def test_support_hold_defers_then_sends_inside_window(h):
    new_gold_customer(h)
    with h.db.engine.begin() as c:
        c.execute(insert(suppressions).values(email="jane@acmecircuits.com", source="support_hold",
                                              reason="open RMA", created_at=utcnow()))
    h.run_range(D(2026, 1, 1), D(2026, 1, 11))
    assert h.apollo.enrollments == []
    with h.db.engine.begin() as c:
        c.execute(suppressions.delete())
    h.run(D(2026, 1, 12))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]


def test_active_in_other_sequence_defers(h):
    new_gold_customer(h)
    c = h.apollo.create_contact({"email": "jane@acmecircuits.com"})
    c["contact_campaign_statuses"].append({"emailer_campaign_id": "tier_a_reactivation", "status": "active"})
    h.run_range(D(2026, 1, 1), D(2026, 1, 12))
    assert h.apollo.enrollments == []
    assert h.touches(acct("10"))["e1"]["reason"] == "deferred:active_in_other_apollo_sequence"


def test_frequency_cap_blocks_back_to_back(tmp_path):
    h = Harness(tmp_path, settings={"eligibility": {"min_days_between_emails": 45}}).strip_verify()
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 2, 25))
    t = h.touches(acct("10"))
    assert t["e1"]["status"] == "sent" and t["e2"]["status"] == "expired"


# ------------------------------------------------------------------ outages, delays, failures
def test_delayed_sync_never_sends_stale_day3_email(h):
    new_gold_customer(h)
    h.run(D(2026, 2, 10))       # system first runs 40 days after purchase
    t = h.touches(acct("10"))
    assert t["e1"]["status"] == "expired"
    assert t["e2"]["status"] == "sent"
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e2"]


def test_qbo_outage_fails_closed_then_recovers(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 1, 7))
    h.qbo.fail = True
    with pytest.raises(RuntimeError):
        h.run(D(2026, 1, 9))
    assert h.apollo.enrollments == []
    assert any("Daily run failed" in a for a in h.alerter.sent)
    h.qbo.fail = False
    h.run(D(2026, 1, 10))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]


def test_outage_longer_than_cdc_window_triggers_full_resync(h):
    new_gold_customer(h)
    h.run(D(2026, 1, 1))
    h.qbo.order("10", D(2026, 1, 20), [("PL-1003", 2, 145.0)])
    h.run(D(2026, 3, 15))       # 73 days later
    assert "query:Invoice" in h.qbo.calls[-6:] or any(c == "query:Invoice" for c in h.qbo.calls[2:])
    assert h.track(acct("10"))["first_reorder_date"] == D(2026, 1, 20)


def test_apollo_transient_failure_retries_next_day_without_duplicates(h):
    new_gold_customer(h)
    h.apollo.fail_enroll_times = 1
    h.run(D(2026, 1, 9))
    assert h.apollo.enrollments == []
    assert h.touches(acct("10"))["e1"]["status"] == "scheduled"
    h.run(D(2026, 1, 10))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]


def test_apollo_permanent_failure_marks_failed_and_alerts(h):
    new_gold_customer(h)
    h.apollo.fail_enroll_times = 99
    h.run_range(D(2026, 1, 9), D(2026, 1, 14))
    assert h.touches(acct("10"))["e1"]["status"] == "failed"
    assert any("failed permanently" in a for a in h.alerter.sent)


def test_crash_between_enroll_and_commit_does_not_double_send(h):
    new_gold_customer(h)
    h.run(D(2026, 1, 8))
    # simulate: Apollo enrolled the contact, then the process died before marking 'sent'
    cont = h.apollo.create_contact({"email": "jane@acmecircuits.com"})
    h.apollo.add_to_sequence("seq_e1", cont["id"], "acct")
    with h.db.engine.begin() as c:
        c.execute(update(touches).where(touches.c.touch_key == "e1").values(
            status="enrolling", apollo_sequence_id="seq_e1", sent_to_email="jane@acmecircuits.com"))
    h.run(D(2026, 1, 9))
    assert h.apollo.sent_to("jane@acmecircuits.com") == ["seq_e1"]
    assert h.touches(acct("10"))["e1"]["status"] == "sent"


def test_day120_live_recheck_catches_reorder_cdc_has_not_seen(h):
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 5, 4))
    tid = h.qbo.order("10", D(2026, 5, 4), [("TS3038", 2, 12.5)])
    h.qbo.cdc_hidden.add(tid)
    h.run(D(2026, 5, 5))
    t = h.touches(acct("10"))
    assert t["e4"]["status"] == "suppressed"
    assert "seq_e4" not in h.apollo.sent_to("jane@acmecircuits.com")


def test_international_order_uses_longer_transit(h):
    h.qbo.customer("95", "Nordic PCB AB", "erik@nordicpcb.se", given="Erik")
    h.qbo.order("95", D(2026, 1, 2), GOLD_KIT, ship_date=D(2026, 1, 2), country="Sweden")
    h.run(D(2026, 1, 2))
    assert h.touches(acct("95"))["e1"]["due_date"] == D(2026, 1, 16)    # +10 transit +4


def test_no_ship_date_falls_back_to_order_date(h):
    h.qbo.customer("96", "Omicron", "pm@omicron.com", given="Ivy")
    h.qbo.order("96", D(2026, 1, 2), GOLD_KIT)
    h.run(D(2026, 1, 2))
    assert h.track(acct("96"))["anchor_basis"].startswith("order_date")


# ------------------------------------------------------------------ safety gates
def test_dry_run_makes_no_apollo_calls(tmp_path):
    h = Harness(tmp_path, mode="dry_run").strip_verify()
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 2, 10))
    assert h.apollo.writes == 0
    with h.db.engine.begin() as c:
        n = c.execute(select(events.c.id).where(events.c.kind == "dry_run_send")).all()
    assert len(n) >= 2
    assert h.touches(acct("10"))["e1"]["status"] == "scheduled" or h.touches(acct("10"))["e1"]["status"] == "expired"


def test_live_blocked_without_approval(tmp_path):
    h = Harness(tmp_path, approve=False).strip_verify()
    new_gold_customer(h)
    with pytest.raises(LiveSendBlocked):
        h.run(D(2026, 1, 9))
    assert h.apollo.enrollments == []


def test_live_blocked_without_env_flag(tmp_path):
    h = Harness(tmp_path, allow=False).strip_verify()
    new_gold_customer(h)
    with pytest.raises(LiveSendBlocked):
        h.run(D(2026, 1, 9))
    assert h.apollo.enrollments == []


def test_unverified_copy_blocks_live_send(tmp_path):
    h = Harness(tmp_path)          # a placeholder left in any email must block that email from going live
    h.cfg.emails["tracks"]["A"]["e2"]["body"] += "\n[[VERIFY: wording still to be confirmed]]\n"
    new_gold_customer(h)
    h.run_range(D(2026, 1, 1), D(2026, 2, 6))
    t = h.touches(acct("10"))
    assert t["e1"]["status"] == "sent"                       # e1 has no markers
    assert t["e2"]["reason"] == "deferred:content_unverified"
    assert any("unverified" in a for a in h.alerter.sent)


def test_sandbox_redirects_and_leaves_touch_for_live(tmp_path):
    h = Harness(tmp_path, mode="sandbox", settings={"sandbox": {"redirect_to": "test@hunterproducts.com"}})
    new_gold_customer(h)
    h.run(D(2026, 1, 9))
    assert h.apollo.sent_to("test@hunterproducts.com") == ["seq_e1"]
    assert h.apollo.sent_to("jane@acmecircuits.com") == []
    subj = h.apollo.find_contact("test@hunterproducts.com")["typed_custom_fields"]["f_hp_subject"]
    assert subj.startswith("[TEST A/e1")
    assert h.touches(acct("10"))["e1"]["status"] == "scheduled"


def test_holdout_group_gets_no_email_but_reorders_are_tracked(tmp_path):
    h = Harness(tmp_path, settings={"analytics": {"holdout_pct": 100}}).strip_verify()
    new_gold_customer(h)
    h.qbo.order("10", D(2026, 2, 15), [("PL-1003", 2, 145.0)])
    h.run_range(D(2026, 1, 1), D(2026, 3, 1))
    tr = h.track(acct("10"))
    assert tr["status"] == "holdout" and tr["reorder_status"] == "replenished"
    assert h.apollo.enrollments == []


def test_report_written_with_metrics(h):
    new_gold_customer(h)
    h.qbo.customer("11", "Repeat Co", "a@repeatco.com", given="Bo")
    h.qbo.order("11", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.qbo.order("11", D(2026, 2, 20), [("PL-1003", 4, 145.0)])
    h.run_range(D(2026, 1, 1), D(2026, 5, 10))
    import json
    m = json.loads((h.tmp / "reports" / "latest.json").read_text())
    assert m["new_customers_identified"] == 2
    assert m["repeat_revenue_after_outreach"] == 580.0
    assert m["repeat_purchase_rates"]["60"]["emailed"] == 0.5
    assert (h.tmp / "reports" / "latest.html").exists()
