"""Contact lookup: who gets the email at each new customer, what role they are, and which version they get."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from hunter_retention.contacts import (MailMessage, name_matches_email, score_inbox, set_manual_contact, ship_to_names, signature_title,
                                       split_name, title_role)
from hunter_retention.db import contacts, events, review_queue
from hunter_retention.render import Renderer

from conftest import FakeApollo, Harness

D = date
GOLD_KIT = [("PL-1000C", 1, 400.0), ("PL-1003", 2, 145.0)]
PATTERNS = [r"(.*[._-])?a[._-]?p([._-].*)?", r".*accounts?[._-]?payable.*", r".*invoic(e|es|ing).*"]


class FakeMail:
    def __init__(self, by_domain=None, by_text=None, fail=False):
        self._d, self._t, self.fail, self.calls = by_domain or {}, by_text or {}, fail, []

    def by_domain(self, domain, since):
        self.calls.append(("domain", domain))
        if self.fail:
            raise RuntimeError("Graph 503")
        return self._d.get(domain, [])

    def by_text(self, text, since):
        self.calls.append(("text", text))
        return self._t.get(text, [])


class PeopleApollo(FakeApollo):
    """Fake Apollo that can also look people up."""

    def __init__(self, people=None):
        super().__init__()
        self.people, self.match_calls = people or {}, 0

    def match_person(self, first_name, last_name, domain, company=None):
        self.match_calls += 1
        return self.people.get((first_name.lower(), last_name.lower(), domain))

    def search_people(self, domain, titles, limit=5):
        return [p for (f, l, d), p in self.people.items() if d == domain and p.get("searchable")][:limit]


def msg(name, email, subject="", text="", to=None, day=D(2025, 11, 1)):
    return MailMessage(name, email, to or [("Hunter", "hunter@hunterproducts.com")], subject, text, day)


def contact_rows(h, key):
    with h.db.engine.begin() as c:
        return {r["email"]: dict(r) for r in c.execute(select(contacts).where(contacts.c.account_key == key)).mappings()}


# ------------------------------------------------------------------ pure helpers
def test_ship_to_names_reads_attn_lines_and_person_lines_but_not_departments():
    assert ship_to_names(("SV Microwave", "Attn: Paul", "2400 Centrepark West Dr."), "SV Microwave") == [("Paul", None)]
    assert ship_to_names(("Timothy Auman", "Auman Machine", "1525 Joel Drive"), "Auman Machine") == [("Timothy", "Auman")]
    assert ship_to_names(("PNC Inc.", "Attn: Lisa/final Insp"), "PNC Inc.") == [("Lisa", None)]
    assert ship_to_names(("L.O. Trading", "Alejandra Zapata", "4340 W. 104th St. #180"), "L.O. Trading") == [("Alejandra", "Zapata")]
    for lines, company in [(("Teledyne Microwave Solutions", "Receiving Department"), "Teledyne Microwave Solutions"),
                           (("Agiliti", "Attn: Receiving"), "Agiliti"), (("Egide S.A.", "Site Sactar"), "Egide S.A."),
                           (("Somacis Inc.", "Raw Material Warehouse"), "Somacis Inc."),
                           (("Moxtek Inc", "452 West 1260 North"), "Moxtek Inc"),
                           (("Winchester Interconnect Corp", "c/o Collectron Int'l Mgmt"), "Winchester Electronics")]:
        assert ship_to_names(lines, company) == [], lines


def test_ship_to_names_rejects_business_and_place_lines_seen_in_real_data():
    cases = [(("Northfield Medical", "x"), "Agiliti Surgical Equipment Repair", {"agilitihealth.com"}),
             (("Vallen Andover", "x"), "Vallen Distribution Inc", {"vallen.com"}),
             (("Logus Mfg", "x"), "Logus Microwave Corporation", {"logus.com"}),
             (("Az Ic.", "Az Pinc"), "AZ Prototype Inc.", {"azpinc.com"}),
             (("Protech Mn", "x"), "Pro-Tech Inc.", {"protechmn.com"}),
             (("Ceva Freight", "x"), "Smooth N Sleek SDN BHD", {"snspcb.com"}),
             (("Vision House", "Bandeath Estate"), "V Tech Smt Ltd", {"vtech-smt.co.uk"})]
    for lines, company, domains in cases:
        assert ship_to_names(lines, company, domains) == [], lines
    assert ship_to_names(("Ty Le", "Green Circuits Inc."), "Green Circuits Inc.", {"greencircuits.com"}) == [("Ty", "Le")]


def test_name_matches_email():
    for first, last, email in [("Thomas", "White", "thomas.a.white@lmco.com"), ("Lance", "Rininger", "lancerininger@ftgcorp.com"),
                               ("Robert", "Trent", "rtrent@kimco.net"), ("Joseph", "Baker", "joe_baker@steris.com"),
                               ("Matt", "Kostyk", "matt@esteemmfg.co"), ("Paul", None, "paul@svmicro.com")]:
        assert name_matches_email(first, last, email), email
    for first, last, email in [("Katherine", "Levin", "kmlange@preferredind.com"), ("Jody", "Barnes", "brenda.jaure@advancedpcb.com"),
                               ("Paul", None, "pauline.k@x.com"), ("Megan", "Gardner", "anita.schober@ttmtech.com")]:
        assert not name_matches_email(first, last, email), email


def test_titles_and_names():
    assert title_role("Sr. Process Engineer") == "user" and title_role("Buyer II") == "buyer"
    assert title_role("Purchasing Manager") == "buyer" and title_role("Office Manager") == "unknown"
    assert split_name("Zapata, Alejandra") == ("Alejandra", "Zapata") and split_name("paul") == ("Paul", None)
    assert signature_title("Thanks,\nDana Reyes\nQuality Engineer\nAcme Circuits\n555-123-4567") == "Quality Engineer"


def test_inbox_scoring_prefers_the_person_who_asked_how_to_use_it():
    msgs = [
        msg("Dana Reyes", "dreyes@acme.com", "Gold pen question",
            "The gold is peeling on our edge connectors. What voltage should we use?\n\nDana Reyes\nProcess Engineer"),
        msg("Sam Ortiz", "sortiz@acme.com", "PO 4500123", "Please see attached purchase order and confirm ship date."),
        msg("AP", "ap@acme.com", "Remittance", "payment sent"),
        msg("Hunter", "hunter@hunterproducts.com", "RE: Gold pen question", "Try 6 to 8 volts.",
            to=[("Dana Reyes", "dreyes@acme.com")]),
        msg("Someone", "x@othercorp.com", "plating", "how to plate"),
    ]
    out = {c.email: c for c in score_inbox(msgs, {"acme.com"}, {"hunterproducts.com"}, PATTERNS, D(2026, 1, 10))}
    assert set(out) == {"dreyes@acme.com", "sortiz@acme.com"}          # AP inbox and other companies never appear
    assert out["dreyes@acme.com"].role == "user" and out["dreyes@acme.com"].score >= 85
    assert out["sortiz@acme.com"].role == "buyer"
    assert out["dreyes@acme.com"].score > out["sortiz@acme.com"].score
    assert out["dreyes@acme.com"].title == "Process Engineer"


def test_product_names_and_order_talk_do_not_make_someone_a_user():
    msgs = [msg("Westak Purchasing", "fmendez@westak.com", "PO 88231 - 24K Gold Micro-Metallizer Plating Pen",
                "Please confirm the purchase order for 10 Gold Plating Pens and Tungsten Carbide Electrodes.\n\n"
                "24K Gold Micro-Metallizer Plating Pen\nWestak Manufacturing"),
            msg("Sen Pham", "sen_pham@jabil.com", "pen", "How do we store the pens? What is the shelf life?\n\nSen Pham\nGIP Operation Procurement"),
            msg("Nisa", "purchasing1@bokopt.com", "RE: order", "What voltage do you recommend?\n\nWe have received the plating pen in good condition.")]
    out = {c.email: c for c in score_inbox(msgs, {"westak.com", "jabil.com", "bokopt.com"}, {"hunterproducts.com"},
                                           PATTERNS, D(2026, 1, 10))}
    assert out["fmendez@westak.com"].role == "buyer" and out["fmendez@westak.com"].first_name is None
    assert out["fmendez@westak.com"].title is None
    assert out["sen_pham@jabil.com"].role == "buyer"            # a procurement title beats a usage question
    assert out["purchasing1@bokopt.com"].role == "buyer" and out["purchasing1@bokopt.com"].title is None


def test_signature_titles_are_strict():
    for text in ["x\n24K Gold Micro-Metallizer Plating Pen", "x\nStreamline Electronics Manufacturing (SEM)",
                 "x\n[UK Authorised Economic Operator]", "x\nWe have received the plating pen in good condition."]:
        assert signature_title(text) is None, text
    assert signature_title("Alex Andos | Purchasing Intern | TTM Technologies") == "Purchasing Intern"
    assert title_role("Accounting Process Analyst") == "unknown" and title_role("Sales Engineer") == "unknown"
    assert title_role("Supplier Quality Engineer") == "user" and title_role("Rework Technician") == "user"


def test_quoted_thread_text_does_not_count_as_their_question():
    m = msg("Sam Ortiz", "sortiz@acme.com", "RE: order", "Thanks, got it.\n\nFrom: Hunter\nUse the degreaser before plating.")
    (c,) = score_inbox([m], {"acme.com"}, {"hunterproducts.com"}, PATTERNS, D(2026, 1, 10))
    assert c.role == "unknown"


# ------------------------------------------------------------------ end to end through the daily job
def test_ap_inbox_only_is_rescued_by_the_engineer_found_in_our_inbox(h):
    h.qbo.customer("10", "Acme Circuits", "ap@acmecircuits.com")
    h.qbo.order("10", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    mail = FakeMail({"acmecircuits.com": [msg("Dana Reyes", "dreyes@acmecircuits.com", "plating question",
                                              "How do we plate gold fingers?\n\nDana Reyes\nProcess Engineer")]})
    h.run(D(2026, 1, 2), mail=mail)
    tr = h.track("acct:10")
    assert tr["status"] == "active" and tr["email"] == "dreyes@acmecircuits.com"
    assert tr["contact_role"] == "user" and tr["contact_source"] == "inbox" and tr["first_name"] == "Dana"
    for d in range(3, 12):
        h.run(D(2026, 1, d), mail=mail)
    assert h.apollo.sent_to("dreyes@acmecircuits.com") == ["seq_e1"]
    assert h.apollo.sent_to("ap@acmecircuits.com") == []
    assert mail.calls.count(("domain", "acmecircuits.com")) == 1        # looked up once, then remembered


def test_without_any_person_the_account_is_still_held_and_never_emails_ap(h):
    h.qbo.customer("11", "Beta Corp", "accountspayable@betacorp.com")
    h.qbo.order("11", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.run_range(D(2026, 1, 1), D(2026, 1, 20))
    tr = h.track("acct:11")
    assert tr["status"] == "held" and tr["status_reason"] == "role_email_only"
    assert h.apollo.enrollments == []


def test_buyer_gets_buyer_versions_and_skips_the_technique_email(h):
    h.qbo.customer("12", "Gamma Devices", "ap@gammadev.com")
    h.qbo.order("12", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1), po="4500778")
    mail = FakeMail(by_text={"4500778": [msg("Sam Ortiz", "sortiz@gammadev.com", "PO 4500778",
                                             "Purchase order attached.\n\nSam Ortiz\nBuyer")]})
    for d in range(1, 32):
        h.run(D(2026, 1, d), mail=mail)
    h.run_range(D(2026, 2, 1), D(2026, 2, 10))
    tr = h.track("acct:12")
    assert tr["contact_role"] == "buyer" and tr["email"] == "sortiz@gammadev.com"
    t = h.touches("acct:12")
    assert t["e1"]["status"] == "sent"
    assert t["e2"]["status"] == "suppressed" and t["e2"]["reason"] == "role:buyer_skips_this_email"
    assert h.apollo.sent_to("sortiz@gammadev.com") == ["seq_e1"]
    with h.db.engine.begin() as c:
        from hunter_retention.db import touches
        subj = c.execute(select(touches.c.rendered_subject).where(touches.c.touch_key == "e1")).scalar()
    assert "PO 4500778" in subj and "Reorder info" in subj


def test_shipping_label_name_is_looked_up_in_apollo_and_credit_is_logged(tmp_path):
    h = Harness(tmp_path).strip_verify()
    h.apollo = PeopleApollo({("lance", "rininger", "ftgcorp.com"): {
        "email": "lrininger@ftgcorp.com", "email_status": "verified", "title": "Process Engineer"}})
    h.qbo.customer("13", "FTG Circuits MN", "ap-invoice@ftgcorp.com")
    h.qbo.order("13", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1),
                ship=["Lance Rininger", "FTG Circuits MN", "11126 Bren Road West"])
    h.run(D(2026, 1, 2))
    tr = h.track("acct:13")
    assert tr["email"] == "lrininger@ftgcorp.com" and tr["contact_role"] == "user" and tr["contact_source"] == "ship_to"
    with h.db.engine.begin() as c:
        assert len(c.execute(select(events.c.id).where(events.c.kind == "apollo_credit")).all()) == 1
    h.run(D(2026, 1, 3))
    assert h.apollo.match_calls == 1                                     # never pays twice for the same account


def test_unverified_apollo_email_is_not_used(tmp_path):
    h = Harness(tmp_path).strip_verify()
    h.apollo = PeopleApollo({("lance", "rininger", "ftgcorp.com"): {
        "email": "lrininger@ftgcorp.com", "email_status": "unverified", "title": "Engineer"}})
    h.qbo.customer("13", "FTG Circuits MN", "ap-invoice@ftgcorp.com")
    h.qbo.order("13", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1), ship=["Lance Rininger", "FTG Circuits MN"])
    h.run(D(2026, 1, 2))
    assert h.track("acct:13")["status"] == "held"


def test_credit_cap_stops_lookups_and_files_a_review_item(tmp_path):
    h = Harness(tmp_path, settings={"contacts": {"monthly_credit_cap": 1}}).strip_verify()
    h.apollo = PeopleApollo({
        ("ann", "lee", "one.com"): {"email": "alee@one.com", "email_status": "verified", "title": "Technician"},
        ("bob", "ray", "two.com"): {"email": "bray@two.com", "email_status": "verified", "title": "Technician"}})
    h.qbo.customer("20", "One Co", "ap@one.com"); h.qbo.customer("21", "Two Co", "ap@two.com")
    h.qbo.order("20", D(2026, 1, 1), GOLD_KIT, ship=["Ann Lee", "One Co"])
    h.qbo.order("21", D(2026, 1, 1), GOLD_KIT, ship=["Bob Ray", "Two Co"])
    h.run(D(2026, 1, 2))
    assert h.apollo.match_calls == 1
    statuses = sorted(h.track(k)["status"] for k in ("acct:20", "acct:21"))
    assert statuses == ["active", "held"]
    with h.db.engine.begin() as c:
        kinds = [r[0] for r in c.execute(select(review_queue.c.kind))]
    assert "contact_suggestion" in kinds


def test_no_apollo_credits_outside_live_mode(tmp_path):
    h = Harness(tmp_path, mode="sandbox").strip_verify()
    h.apollo = PeopleApollo({("ann", "lee", "one.com"): {"email": "alee@one.com", "email_status": "verified"}})
    h.qbo.customer("20", "One Co", "ap@one.com")
    h.qbo.order("20", D(2026, 1, 1), GOLD_KIT, ship=["Ann Lee", "One Co"])
    h.run(D(2026, 1, 2))
    assert h.apollo.match_calls == 0 and h.track("acct:20")["status"] == "held"


def test_person_already_in_quickbooks_is_kept_and_upgraded_by_inbox_evidence(h):
    h.qbo.customer("30", "Delta Labs", "jane@deltalabs.com, ap@deltalabs.com", given="Jane")
    h.qbo.order("30", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    mail = FakeMail({"deltalabs.com": [msg("Jane Buyer", "jane@deltalabs.com", "order",
                                           "Please confirm lead time for the purchase order.\n\nJane\nPurchasing Agent")]})
    h.run(D(2026, 1, 2), mail=mail)
    tr = h.track("acct:30")
    assert tr["email"] == "jane@deltalabs.com" and tr["contact_role"] == "buyer"


def test_quickbooks_person_named_on_the_shipping_label_is_a_user_with_no_apollo_call(tmp_path):
    h = Harness(tmp_path).strip_verify()
    h.apollo = PeopleApollo()
    h.qbo.customer("40", "Kimco Design", "rtrent@kimco.net, ap@kimco.net")
    h.qbo.order("40", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1), ship=["Robert Trent", "Kimco Design"])
    h.run(D(2026, 1, 2))
    tr = h.track("acct:40")
    assert tr["contact_role"] == "user" and tr["email"] == "rtrent@kimco.net" and tr["first_name"] == "Robert"
    assert h.apollo.match_calls == 0


def test_buyer_named_on_the_shipping_label_stays_a_buyer(h):
    h.qbo.customer("43", "Pacaero", "sdunning@pacaero.com", given="Sandra Dunning")
    h.qbo.order("43", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1), ship=["Sandra Dunning", "Pacaero"])
    mail = FakeMail({"pacaero.com": [msg("Sandra Dunning", "sdunning@pacaero.com", "PO 7781",
                                         "Purchase order attached, please confirm ship date.")]})
    h.run(D(2026, 1, 2), mail=mail)
    tr = h.track("acct:43")
    assert tr["contact_role"] == "buyer" and tr["first_name"] == "Sandra"


def test_shared_purchasing_mailbox_is_treated_as_a_buyer(h):
    h.qbo.customer("41", "Ampel Inc.", "purchasing@ampelinc.com", given="Andy")
    h.qbo.order("41", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.run(D(2026, 1, 2))
    tr = h.track("acct:41")
    assert tr["contact_role"] == "buyer" and tr["email"] == "purchasing@ampelinc.com"


def test_personal_mailbox_at_a_company_with_its_own_domain_is_not_an_owner_operator(h):
    h.qbo.customer("42", "Big Medical Inc.", "someone@gmail.com, big_ap@bigmedical.com")
    h.qbo.order("42", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.run(D(2026, 1, 2))
    assert h.track("acct:42")["contact_role"] == "unknown"


def test_owner_operator_is_detected_from_a_personal_mailbox(h):
    h.qbo.customer("31", "Mike's Restorations", "mike.r@gmail.com", given="Mike")
    h.qbo.order("31", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.run(D(2026, 1, 2))
    assert h.track("acct:31")["contact_role"] == "owner_operator"


def test_weak_engineer_suggestion_goes_to_review_and_does_not_replace_the_known_contact(h):
    h.qbo.customer("32", "Echo Systems", "pat@echosys.com", given="Pat")
    h.qbo.order("32", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    weak = MailMessage("Lee Chan", "lchan@echosys.com", [("Hunter", "hunter@hunterproducts.com")], "hello",
                       "Lee Chan\nTest Technician", D(2023, 1, 1))
    h.run(D(2026, 1, 2), mail=FakeMail({"echosys.com": [weak]}))
    assert h.track("acct:32")["email"] == "pat@echosys.com"
    rows = contact_rows(h, "acct:32")
    assert rows["lchan@echosys.com"]["status"] == "suggested" and rows["pat@echosys.com"]["status"] == "primary"


def test_manual_choice_always_wins_over_later_lookups(h):
    h.qbo.customer("33", "Foxtrot Inc", "ap@foxtrot.com")
    h.qbo.order("33", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    h.run(D(2026, 1, 2))
    set_manual_contact(h.db, "acct:33", "Kim@Foxtrot.com", "user", "Kim", "Park")
    mail = FakeMail({"foxtrot.com": [msg("Al Buyer", "al@foxtrot.com", "plating", "how to plate? what voltage?")]})
    h.run(D(2026, 1, 3), mail=mail)
    tr = h.track("acct:33")
    assert tr["status"] == "active" and tr["email"] == "kim@foxtrot.com" and tr["contact_role"] == "user"
    assert tr["first_name"] == "Kim"


def test_suppressed_address_is_never_picked(h):
    from hunter_retention.db import suppressions, utcnow
    with h.db.engine.begin() as c:
        c.execute(suppressions.insert().values(email="dreyes@golf.com", reason="asked", source="manual", created_at=utcnow()))
    h.qbo.customer("34", "Golf Co", "ap@golf.com")
    h.qbo.order("34", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    mail = FakeMail({"golf.com": [msg("Dana Reyes", "dreyes@golf.com", "plating", "how to plate gold?")]})
    h.run(D(2026, 1, 2), mail=mail)
    assert h.track("acct:34")["status"] == "held"


def test_mailbox_outage_does_not_break_the_daily_run(h):
    h.qbo.customer("35", "Hotel Corp", "jo@hotelcorp.com", given="Jo")
    h.qbo.order("35", D(2026, 1, 1), GOLD_KIT, ship_date=D(2026, 1, 1))
    stats = h.run(D(2026, 1, 2), mail=FakeMail(fail=True))
    assert stats["contacts"]["mail_errors"] == 1 and h.track("acct:35")["email"] == "jo@hotelcorp.com"


def test_historical_customers_are_not_looked_up(h):
    h.qbo.customer("36", "Old Co", "ap@oldco.com")
    h.qbo.order("36", D(2024, 5, 1), GOLD_KIT)
    mail = FakeMail()
    stats = h.run(D(2026, 1, 2), mail=mail)
    assert stats["contacts"]["looked_up"] == 0 and mail.calls == []


# ------------------------------------------------------------------ copy versions
def _track(h, role, seg="A"):
    from hunter_retention.cli import sample_track
    tr = sample_track(h.cfg, seg, "gold" if seg == "A" else None)
    tr["contact_role"] = role
    tr["order_summary"]["po_number"] = "613707"
    return tr


def test_every_role_renders_cleanly_for_every_segment_and_email(h):
    r = Renderer(h.cfg)
    for seg in "ABCD":
        for t in h.cfg.lc(f"tracks.{seg}.touches"):
            for role in ("user", "buyer", "owner_operator", "unknown"):
                out = r.render(_track(h, role, seg), t["key"])
                if out.skip:
                    assert (role, t["key"]) == ("buyer", "e2")
                    continue
                assert out.is_clean and out.subject and "{{" not in out.body, (seg, t["key"], role)
                assert "—" not in out.subject + out.body                      # house rule: no em dashes


def test_buyer_copy_differs_and_user_copy_is_the_standard_one(h):
    r = Renderer(h.cfg)
    buyer, user, unknown = (r.render(_track(h, x), "e3") for x in ("buyer", "user", "unknown"))
    assert "lead time" in buyer.subject.lower() and "Net 30" in buyer.body and buyer.variant == "buyer"
    assert user.body == unknown.body and user.variant == "standard"
    assert "50 pens" in r.render(_track(h, "buyer"), "e4").body
    assert "50 pens" not in r.render(_track(h, "buyer", "B"), "e4").body
    assert "web store" in r.render(_track(h, "owner_operator"), "e3").body
