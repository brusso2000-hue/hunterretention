"""Reorder flow: existing customers who go longer than usual without ordering."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from hunter_retention.db import review_queue, touches, tracks

from conftest import Harness

D = date
PENS = [("PL-1003", 4, 145.0), ("PL-1006", 2, 70.0)]
ON = {"reorder": {"start_date": "2025-01-01"}}


def repeat_customer(h, cid="50", email="dana@acme.com", every=60, n=4, last=D(2025, 9, 1), amount=None, po=None):
    """A customer who has ordered n times, `every` days apart, most recently on `last`."""
    h.qbo.customer(cid, f"Acme {cid}", email, given="Dana")
    lines = PENS if amount is None else [("PL-1003", 1, amount)]
    for i in range(n):
        h.qbo.order(cid, last - timedelta(days=every * (n - 1 - i)), lines, po=po)
    return f"acct:{cid}"


def episodes(h, key):
    with h.db.engine.begin() as c:
        return [dict(r) for r in c.execute(select(tracks).where(
            (tracks.c.account_key == key) & (tracks.c.flow == "reorder")).order_by(tracks.c.id)).mappings()]


def ep_touches(h, tid):
    with h.db.engine.begin() as c:
        return {r["touch_key"]: dict(r) for r in c.execute(select(touches).where(touches.c.track_id == tid)).mappings()}


def reviews(h):
    with h.db.engine.begin() as c:
        return [r[0] for r in c.execute(select(review_queue.c.kind))]


def test_customer_is_added_the_day_they_pass_their_threshold_and_not_before(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, every=60, last=D(2025, 9, 1))          # threshold = 60 x 1.3 = 78 days -> Nov 18
    h.run(D(2025, 11, 17))
    assert episodes(h, key) == []
    h.run(D(2025, 11, 18))
    (ep,) = episodes(h, key)
    assert ep["status"] == "active" and ep["segment"] == "R" and ep["order_summary"]["usual_gap_days"] == 60
    assert h.apollo.sent_to("dana@acme.com") == ["seq_r1"]


def test_three_emails_then_done_and_never_restarted_for_the_same_gap(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h)
    h.run_range(D(2025, 11, 18), D(2026, 1, 20))
    assert h.apollo.sent_to("dana@acme.com") == ["seq_r1", "seq_r2", "seq_r3"]
    (ep,) = episodes(h, key)                                         # still exactly one episode
    assert ep["status"] == "completed"
    t = ep_touches(h, ep["id"])
    assert (t["r2"]["sent_on"] - t["r1"]["sent_on"]).days == 14 and (t["r3"]["sent_on"] - t["r1"]["sent_on"]).days == 30


def test_a_new_order_stops_the_remaining_emails(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h)
    h.run_range(D(2025, 11, 18), D(2025, 11, 22))
    h.qbo.order("50", D(2025, 11, 23), PENS)
    h.run_range(D(2025, 11, 23), D(2026, 1, 10))
    assert h.apollo.sent_to("dana@acme.com") == ["seq_r1"]
    (ep,) = episodes(h, key)
    assert ep["status"] == "converted" and ep["reorder_status"] == "reordered"
    assert ep_touches(h, ep["id"])["r2"]["status"] == "suppressed"


def test_it_is_ongoing_the_same_customer_comes_back_in_after_the_next_long_gap(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h)
    h.run_range(D(2025, 11, 18), D(2025, 11, 20))
    h.qbo.order("50", D(2025, 11, 21), PENS)                         # they reorder...
    h.run_range(D(2025, 11, 21), D(2026, 3, 15))                     # ...then go quiet again
    eps = episodes(h, key)
    assert len(eps) == 2 and eps[0]["status"] == "converted" and eps[1]["episode_key"] != eps[0]["episode_key"]
    assert h.apollo.sent_to("dana@acme.com").count("seq_r1") == 2


def test_fewer_than_three_orders_has_no_rhythm(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, n=2)
    h.run_range(D(2025, 11, 18), D(2025, 12, 30))
    assert episodes(h, key) == [] and h.apollo.enrollments == []


def test_key_accounts_are_flagged_for_a_personal_note_not_emailed(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, amount=6000.0)                          # 2 orders in the last 12 months = $12,000
    h.run_range(D(2025, 11, 18), D(2025, 11, 25))
    assert episodes(h, key) == [] and h.apollo.enrollments == []
    assert reviews(h).count("reorder_key_account") == 1              # flagged once, not every day


def test_long_gone_customers_are_flagged_not_emailed(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, every=60, last=D(2025, 1, 1))
    h.run(D(2025, 11, 18))                                           # 321 days vs a 60-day habit
    assert episodes(h, key) == [] and "reorder_long_gone" in reviews(h)


def test_customers_gone_for_years_are_left_alone_without_a_flag(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, every=300, last=D(2023, 6, 1))
    h.run(D(2025, 11, 18))                                           # 901 days since the last order
    assert episodes(h, key) == [] and reviews(h) == []


def test_uneven_buyers_are_flagged_not_emailed(tmp_path):
    h = Harness(tmp_path, settings=ON).strip_verify()
    h.qbo.customer("51", "Lumpy Co", "lee@lumpy.com", given="Lee")
    for d in (D(2023, 1, 1), D(2023, 1, 20), D(2024, 12, 1), D(2024, 12, 15), D(2025, 6, 1)):
        h.qbo.order("51", d, PENS)
    h.run(D(2025, 11, 18))
    assert episodes(h, "acct:51") == [] and "reorder_irregular" in reviews(h)


def test_nothing_starts_before_the_start_date(tmp_path):
    h = Harness(tmp_path, settings={"reorder": {"start_date": "2026-06-01"}}).strip_verify()
    key = repeat_customer(h)
    h.run_range(D(2025, 11, 18), D(2025, 11, 25))
    assert episodes(h, key) == [] and h.apollo.enrollments == []


def test_customer_in_the_first_time_flow_is_left_to_that_flow(tmp_path):
    h = Harness(tmp_path, settings={"reorder": {"start_date": "2025-01-01", "min_orders": 2, "min_usual_gap_days": 1}}).strip_verify()
    h.qbo.customer("52", "New Co", "nia@newco.com", given="Nia")     # first purchase inside the program
    h.qbo.order("52", D(2026, 1, 1), [("PL-1000C", 1, 400.0), ("PL-1003", 2, 145.0)], ship_date=D(2026, 1, 1))
    h.qbo.order("52", D(2026, 1, 8), PENS)
    h.qbo.order("52", D(2026, 1, 15), PENS)
    h.run_range(D(2026, 1, 15), D(2026, 2, 20))
    assert episodes(h, "acct:52") == []
    assert all(s.startswith("seq_e") for s in h.apollo.sent_to("nia@newco.com"))


def test_ap_inbox_only_is_held_then_released_when_a_contact_is_found(tmp_path):
    from hunter_retention.contacts import set_manual_contact
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, email="ap@acme.com")
    h.run(D(2025, 11, 18))
    (ep,) = episodes(h, key)
    assert ep["status"] == "held" and h.apollo.enrollments == []
    set_manual_contact(h.db, key, "dana@acme.com", "buyer", "Dana")
    h.run(D(2025, 11, 19))
    assert episodes(h, key)[0]["status"] == "active" and h.apollo.sent_to("dana@acme.com") == ["seq_r1"]
    assert h.apollo.sent_to("ap@acme.com") == []


def test_buyer_gets_the_buyer_version_with_quantities_and_po(tmp_path):
    from hunter_retention.contacts import set_manual_contact
    h = Harness(tmp_path, settings=ON).strip_verify()
    key = repeat_customer(h, po="4500778")
    set_manual_contact(h.db, key, "dana@acme.com", "buyer", "Dana")
    h.run(D(2025, 11, 18))
    t = ep_touches(h, episodes(h, key)[0]["id"])["r1"]
    assert "last PO 4500778" in t["rendered_subject"] and "4 x 24K Gold" in t["rendered_body"]
    assert "September 1, 2025" in t["rendered_body"]


def test_threshold_can_be_a_fixed_number_of_days_past_usual(tmp_path):
    h = Harness(tmp_path, settings={"reorder": {"start_date": "2025-01-01", "trigger_multiple": 1.0,
                                                "trigger_extra_days": 60}}).strip_verify()
    key = repeat_customer(h, every=60, last=D(2025, 9, 1))           # 60 + 60 = 120 days -> Dec 30
    h.run(D(2025, 12, 29))
    assert episodes(h, key) == []
    h.run(D(2025, 12, 30))
    assert len(episodes(h, key)) == 1


def test_dry_run_sends_nothing(tmp_path):
    h = Harness(tmp_path, mode="dry_run", settings=ON).strip_verify()
    key = repeat_customer(h)
    stats = h.run(D(2025, 11, 18))
    assert stats["reorder"]["started"] == 1 and stats["dispatch"]["dry_run"] == 1 and h.apollo.enrollments == []


def test_simulation_runs_both_flows_without_writing_to_the_source_database(tmp_path):
    from hunter_retention.simulate import simulate
    h = Harness(tmp_path, mode="dry_run").strip_verify()
    today = D(2026, 3, 1)
    repeat_customer(h, cid="60", email="dana@acme.com", every=60, last=today - timedelta(days=95))      # overdue
    repeat_customer(h, cid="61", email="lee@beta.com", every=60, last=today - timedelta(days=20))       # on time
    h.qbo.customer("62", "New Co", "nia@newco.com", given="Nia")                                        # first-time
    h.qbo.order("62", today - timedelta(days=40), [("PL-1000C", 1, 400.0), ("PL-1003", 2, 145.0)])
    h.run(D(2024, 1, 1))                                 # sync only: far before any program date
    with h.db.engine.begin() as c:
        before = len(c.execute(select(tracks)).all())
    out = simulate(h.cfg, h.db, today, 60, tmp_path / "sim")
    assert out["errors"] == 0
    assert out["first_time"]["customers"] == 1 and "e1" in out["first_time"]["emails_would_have_sent"]
    assert out["reorder"]["customers"] == 1 and "r1" in out["reorder"]["emails_would_have_sent"]
    with h.db.engine.begin() as c:
        assert len(c.execute(select(tracks)).all()) == before          # source database untouched
    assert (tmp_path / "sim" / "sim_emails.csv").exists()
