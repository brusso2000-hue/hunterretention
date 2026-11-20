"""Unit tests: classifier against the real QuickBooks catalog, identity rules, rendering, QBO token rotation."""
from __future__ import annotations

import json

import pytest

from hunter_retention.cli import sample_track
from hunter_retention.classify import IGNORED, LIFECYCLE, NON_PRODUCT, UNCLASSIFIED, Classifier, Line
from hunter_retention.config import load_config
from hunter_retention.db import DB
from hunter_retention.identity import resolve_accounts
from hunter_retention.qbo import QBOClient, TokenStore
from hunter_retention.render import Renderer

# Every item name in Hunter's QuickBooks Product/Service list (pulled 2026-11-17)
REAL_CATALOG = """Out of State (deleted)|NJ Sales Tax (deleted)|Shipping|PL1003 (deleted)|Discount|TS3037-110 Volt|TS3053|
Bank Charges|TS3037|HUNPL-1003 (deleted)|HUNPL-1006 (deleted)|HUNPL-1008 (deleted)|PL-1002|PL-1005|PL-1008|PL-1003|AB2004|
AB2002|TS3038|TS3039|TS3040|PL-1016|PL-1001|TS3041|PL-1015|PL-1012|PL-1004|PL-1006|TS3046|TS3045|TS3042|AC-12|PL-1014|
PL-1018|PL-1000C|HUNPL-1018 (deleted)|PL-1000   110volt|PL-1010|PL-1000K|PL-1020|M8A|HUNPL-1002 (deleted)|PL-1017|PL-1013|
HUN-PL-1010 (deleted)|M.7|HUNPL-1001 (deleted)|HUNPL-1007 (deleted)|#100 Aliminum Oxiide (deleted)|#100 Aluiminum Oxiide|
HUNPL-1008-1 (deleted)|MT6004|PL-1007|PL-1000|AB2034|AB2010|AB2016|AB2009|AB2007|AB2000|AB2028|MT6008|M8B|
HUNPL-1012 (deleted)|M.5|AB2029|PL-1019|AB2012|PL-1000CHD|TS3044|MT6004 - 4|Shipping1 (deleted)|NJ Sales Tax1 (deleted)|
HUNPL-1020 (deleted)|Shiipping|AB2025|AB2013|AB2001|AB2005|AB2017|Discounts|PL-1011|AB2024|M.9|M.8A|PL-1002,PL-1007,PL-1016|
AB2015|PL1002|PL1007|PL-1003a (deleted)|PL-1003-1|AB2018|AB2019|AB2006|AB2008|AB2023|AB2011|NJ Sales Tax 2018 (deleted)|
Bank Fees|AB3032|Duties and Taxes|PL-1000HD|- (deleted)|AB2014|AB2021|AB2022|AB2003|CA (deleted)|CA State sales tax (deleted)|
MT6009|CA 8.25% (deleted)|PL-1009|CA Santa Ana (deleted)|Services|PmntDiscount_402 - Discounts|PmntDiscount_713 - Bank Charges|
TS3050|AB2032|RM Tariffs|Surcharge|Tariff surcharge|Late fee|Credit"""


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def clf(cfg):
    return Classifier(cfg.products, cfg.lifecycle)


def test_real_catalog_fully_classified_except_known_unknowns(clf):
    names = [n.strip() for n in REAL_CATALOG.replace("\n", "").split("|")]
    assert len(names) == 123
    unclassified = [n for n in names if clf.classify_item(None, n).status == UNCLASSIFIED]
    assert unclassified == []


@pytest.mark.parametrize("name,family,role,apps", [
    ("PL-1003", "micro_metallizer", "consumable", ("gold",)),
    ("PL1003 (deleted)", "micro_metallizer", "consumable", ("gold",)),
    ("PL-1016", "micro_metallizer", "consumable", ("gold",)),
    ("PL-1007", "micro_metallizer", "consumable", ("nickel",)),
    ("PL-1012", "micro_metallizer", "consumable", ("tin",)),
    ("PL-1000   110volt", "micro_metallizer", "equipment", ()),
    ("PL-1000C", "micro_metallizer", "equipment", ()),
    ("TS3037-110 Volt", "carbitron", "equipment", ()),
    ("TS3040", "carbitron", "consumable", ()),
    ("AB2000", "micro_jet", "equipment", ()),
    ("AB2029", "micro_jet", "consumable", ()),
    ("AB2010", "micro_jet", "accessory", ()),
])
def test_item_mapping(clf, name, family, role, apps):
    ic = clf.classify_item(None, name)
    assert (ic.status, ic.family, ic.role, ic.applications) == (LIFECYCLE, family, role, apps)


def test_non_product_and_ignored(clf):
    assert clf.classify_item(None, "Shipping").status == NON_PRODUCT
    assert clf.classify_item(None, "Tariff surcharge").status == NON_PRODUCT
    assert clf.classify_item(None, "CA Santa Ana (deleted)").status == NON_PRODUCT
    assert clf.classify_item(None, "M.5").status == IGNORED
    assert clf.classify_item(None, "Carbitron-like-name").status == UNCLASSIFIED


def test_item_id_override_beats_name(cfg):
    products = json.loads(json.dumps(cfg.products))
    products["items"].insert(0, {"qbo_item_id": "999", "family": "carbitron", "role": "equipment"})
    c = Classifier(products, cfg.lifecycle)
    assert c.classify_item("999", "PL-1003").family == "carbitron"


def _line(clf, name):
    return Line(None, name, None, 1, 10, clf.classify_item(None, name))


def test_segment_priority(clf):
    oc = clf.classify_lines([_line(clf, "PL-1003"), _line(clf, "AB2000")])
    assert oc.segment == "C" and oc.secondary_segments == ["A"]
    oc = clf.classify_lines([_line(clf, "TS3038")])
    assert oc.segment == "D" and oc.subsegment == "carbitron"
    oc = clf.classify_lines([_line(clf, "PL-1002")])                      # degreaser only
    assert oc.segment == "D"
    oc = clf.classify_lines([_line(clf, "PL-1000K")])                     # kit only: no application guessed
    assert oc.segment == "A" and oc.subsegment == "general"
    oc = clf.classify_lines([_line(clf, "Shipping")])
    assert oc.is_unclassified


def test_identity_freemail_not_merged():
    custs = [
        {"id": "1", "display_name": "Shop One", "company_name": "Shop", "email_raw": "a@gmail.com", "parent_id": None},
        {"id": "2", "display_name": "Shop Two", "company_name": "Shop", "email_raw": "b@gmail.com", "parent_id": None},
    ]
    accts, _ = resolve_accounts(custs, [], {"gmail.com"})
    assert len(accts) == 2


def test_identity_multiple_emails_and_role_split():
    custs = [{"id": "5", "display_name": "X", "company_name": "X Co", "given_name": "Ann",
              "email_raw": "ap@xco.com, ann@xco.com", "parent_id": None}]
    accts, _ = resolve_accounts(custs, ["ap"], set())
    a = accts["acct:5"]
    assert a.contact_email == "ann@xco.com" and a.role_emails == ["ap@xco.com"]


def test_every_template_renders_for_every_segment(cfg):
    r = Renderer(cfg)
    combos = [("A", a) for a in ("gold", "nickel", "tin", "silver", "copper", "other", "gold+nickel", None)] + \
             [("B", None), ("C", None), ("D", "carbitron"), ("D", "micro_jet"), ("D", "micro_metallizer")]
    for seg, app in combos:
        for t in cfg.lc(f"tracks.{seg}.touches"):
            out = r.render(sample_track(cfg, seg, app), t["key"])
            assert out.subject and out.body
            assert "{{" not in out.body and "—" not in out.body + out.subject     # no em dashes
            assert "jewel" not in out.body.lower()


def test_no_first_name_uses_neutral_greeting(cfg):
    tr = sample_track(cfg, "A", "gold")
    tr["first_name"] = None
    out = Renderer(cfg).render(tr, "e1")
    assert out.body.startswith("Hello,")


def test_e1_never_claims_delivery(cfg):
    for seg in ("A", "B", "C", "D"):
        out = Renderer(cfg).render(sample_track(cfg, seg, "gold" if seg == "A" else None), "e1")
        low = out.body.lower()
        assert "arrived" not in low and "delivered" not in low and "received your" not in low


# ------------------------------------------------------------------ QBO token rotation
class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.headers = status, payload, {}
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._p


class _Session:
    def __init__(self):
        self.calls = []
        self.n = 0

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if "tokens/bearer" in url:
            self.n += 1
            return _Resp(200, {"access_token": f"AT{self.n}", "refresh_token": f"RT{self.n}", "expires_in": 3600,
                               "x_refresh_token_expires_in": 8640000})
        return _Resp(200, {"QueryResponse": {"Invoice": [{"Id": "1"}]}})


def test_qbo_refresh_token_rotation_is_persisted(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/q.db"); db.create_all()
    store = TokenStore(db)
    sess = _Session()
    client = QBOClient("cid", "secret", "123", store, bootstrap_refresh_token="RT0", session=sess)
    rows = list(client.query_all("Invoice"))
    assert rows == [{"Id": "1"}]
    assert store.load()["refresh_token"] == "RT1"          # rotated token saved
    assert sess.calls[0][2]["data"]["refresh_token"] == "RT0"
    assert "minorversion" in sess.calls[1][2]["params"]


def test_real_world_ap_addresses_are_detected(cfg):
    from hunter_retention.util import is_role_address
    pats = cfg.s("identity.role_address_patterns")
    for e in ("aesculap_ap@aesculapusa.com", "apforestgrove@ttmtech.com", "accounts.payable@x.com", "invoices@x.com"):
        assert is_role_address(e, pats), e
    for e in ("cl_smajda@fischione.com", "april@x.com", "aparna@x.com", "purchasing@x.com"):
        assert not is_role_address(e, pats), e


def test_intuit_tid_captured_on_errors():
    from hunter_retention.http import ApiError, request_with_retry

    class R:
        status_code, text, content = 400, "bad query", b"x"
        headers = {"intuit_tid": "1-abc-123"}

    class S:
        def request(self, *a, **k):
            return R()

    with pytest.raises(ApiError) as e:
        request_with_retry(S(), "GET", "https://x", sleep=lambda s: None)
    assert e.value.intuit_tid == "1-abc-123" and "1-abc-123" in str(e.value)


def test_test_send_versions_and_own_domain_guard(cfg, capsys):
    from types import SimpleNamespace
    from hunter_retention.cli import cmd_test_send, test_versions
    core = test_versions(cfg, "core")
    assert len(test_versions(cfg, "smoke")) == 1
    assert {t for _, t, _ in core} == {"e1", "e2", "e3", "e4", "r1", "r2", "r3"}
    assert len({tag for tag, _, _ in core}) == len(core)          # one plus-address per version
    assert len(test_versions(cfg, "all")) > len(core)
    cfg.settings["sandbox"]["redirect_to"] = "someone@customer.com"
    assert cmd_test_send(cfg, SimpleNamespace(set="smoke", format=None, print_only=True)) == 1
    assert "refusing" in capsys.readouterr().out
