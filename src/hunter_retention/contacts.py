"""Contact lookup: find the right person to email at each first-time customer, and tag what they are.

QuickBooks usually holds the address invoices go to (often an accounts-payable inbox), not the person
using the product. For each new first-time buyer this module looks, in order, at:

  1. qbo         the personal addresses already on the QuickBooks record            (free)
  2. inbox       everyone at the customer's domain who has emailed us, scored        (free)
  3. po_email    whoever sent us the purchase order named on the invoice             (free)
  4. ship_to     the person named on the shipping label, looked up in Apollo         (1 credit)
  5. apollo_search  an engineer at that company, for larger orders only              (1 credit, review only)

Every candidate gets a role (user | buyer | owner_operator | unknown) and a 0-100 score. The best
candidate at or above `contacts.auto_threshold` becomes the account's primary contact automatically;
weaker finds are filed as suggestions for a human. The answer is stored once per account.

Hard rules: never a role inbox (ap@, billing@, no-reply@), never an address outside the customer's own
domain, never a suppressed address, never more Apollo credits than `contacts.monthly_credit_cap`.
Only names, addresses, titles and one-line reasons are stored. Email text is read to score it and
then discarded.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, insert, select, update

from .classify import Classifier
from .config import Config
from .db import DB, contacts, events, suppressions, utcnow
from .identity import Account
from .util import email_domain, is_role_address, normalize_company
from .world import FirstPurchase, World, apply_contacts, first_purchase

log = logging.getLogger(__name__)

ROLES = ("user", "buyer", "owner_operator", "unknown")
ROLE_RANK = {"user": 0, "owner_operator": 0, "buyer": 1, "unknown": 2}

# Job titles. A user title needs a hands-on role noun: words like "plating" or "manufacturing" alone
# also appear in product and company names, so they do not count.
USER_TITLE = re.compile(
    r"\b(engineer|technician|technologist|inspector|scientist|chemist|machinist|toolmaker|assembler|plater|"
    r"(production|process|manufacturing|plating|rework|repair|maintenance|quality|lab|laboratory|shop|"
    r"operations) (manager|supervisor|lead|leader|specialist|tech|operator|coordinator)|machine operator|"
    r"quality (assurance|control)|r&d)\b", re.I)
BUYER_TITLE = re.compile(
    r"\b(buyer|purchasing|procurement|purchaser|supply chain|sourcing|planner|commodity|expedit\w*|"
    r"materials? (manager|coordinator|specialist|analyst))\b", re.I)
# Titles that look technical but are not the person using the product.
OTHER_TITLE = re.compile(r"\b(sales|account(s|ing|ant)?|customer service|receptionist|payable|controller|"
                         r"bookkeep\w*|logistics|shipping|receiving)\b", re.I)
PRODUCT_WORDS = re.compile(r"\b(pen|pens|kit|micro-?metallizer|carbitron|micro-?jet|gold|nickel|24k|order|invoice|"
                           r"received|attached|thank\w*|regards|please)\b", re.I)
# Signs someone is using the product, not just ordering it. Product names ("Gold Plating Pen") must not
# match, so bare words like "plating" or "electrode" are left out on purpose.
TECH_TALK = re.compile(
    r"\b(adhesion|adhere\w*|peel(ing|s|ed)?|flak\w+|thickness|micro-?inch\w*|voltage|volts?|amperage|"
    r"how (do|to|long|many|much|thick)|instructions?|not (working|plating|sticking|adhering)|won'?t plate|"
    r"having (trouble|problems?|issues?)|substrate|surface prep\w*|masking|gold fingers?|contact pads?|"
    r"burn(ing|t|ed)|dull finish|discolor\w*|tarnish\w*|coverage|which pen|what pen|recommend\w*|"
    r"shelf life|tips? (dr(y|ied)|wear|worn)|technique|troubleshoot\w*|application (question|help))\b", re.I)
PO_TALK = re.compile(
    r"\b(purchase order|p\.?\s?o\.?\s?(#|no\.?|number)?\s?\d|order (confirmation|acknowledg\w+)|"
    r"acknowledg\w+|remit\w*|payment terms|net ?30|w-?9|vendor (form|setup|registration)|ship date|"
    r"tracking number|lead ?time|price (list|quote)|quotation|invoice)\b", re.I)

_NOT_A_NAME = {
    "attn", "attention", "receiving", "shipping", "purchasing", "accounts", "payable", "dock", "warehouse",
    "dept", "department", "stores", "stockroom", "lab", "laboratory", "inspection", "final", "site", "plant",
    "raw", "material", "materials", "building", "bldg", "unit", "suite", "floor", "division", "facility",
    "center", "centre", "po", "box", "road", "street", "drive", "avenue", "blvd", "lane", "way", "inc", "llc",
    "corp", "ltd", "co", "company", "electronics", "systems", "technologies", "manufacturing", "circuits",
    "industries", "group", "international", "north", "south", "east", "west", "usa", "america",
    "medical", "microelectronics", "crescent", "city", "solutions", "interconnect", "freight", "crib", "house",
    "estate", "zone", "place", "builiding", "mfg", "laboratories", "labs", "fasteners", "circuit", "source",
    "avenida", "calle", "real", "san", "santa", "saint", "fort", "park", "industrial", "technology", "tech",
    "sales", "service", "services", "design", "tool", "tools", "products", "supply", "trading", "distribution",
    "qc", "qa", "tx", "ph", "il", "mn", "ca", "ny", "andover", "the", "and", "of", "for", "sdn", "bhd", "pte",
}
# A shared purchasing mailbox is a buyer, not a person who uses the product.
PURCHASING_MAILBOX = re.compile(r"^(gc)?(purchas\w*|procurement|buyers?|buying|compras|supplier|orders?)[\d._-]*\w{0,8}$", re.I)
_ATTN = re.compile(r"^\s*(?:attn|attention)\b[\s:.\-]*(.+)$", re.I)
_CARE_OF = re.compile(r"^\s*c/o\b", re.I)          # "c/o" names a forwarding company, not a person
_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'\-]+\.?$")


# ----------------------------------------------------------------------------- sources
@dataclass
class MailMessage:
    sender_name: str | None
    sender_email: str
    recipients: list[tuple[str | None, str]]
    subject: str
    text: str                       # body text; used for scoring only, never stored
    date: date | None = None


class MailSearch(Protocol):
    def by_domain(self, domain: str, since: date) -> list[MailMessage]: ...
    def by_text(self, text: str, since: date) -> list[MailMessage]: ...


class PeopleLookup(Protocol):
    def match_person(self, first_name: str, last_name: str, domain: str, company: str | None = None) -> dict | None: ...
    def search_people(self, domain: str, titles: list[str], limit: int = 5) -> list[dict]: ...


# ----------------------------------------------------------------------------- results
@dataclass
class Candidate:
    email: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    role: str = "unknown"
    source: str = "qbo"
    score: int = 0
    evidence: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


@dataclass
class Resolution:
    account_key: str
    company: str | None
    primary: Candidate | None
    suggested: list[Candidate]
    candidates: list[Candidate]
    would_look_up: list[str]         # Apollo lookups skipped (no key, preview, or credit cap)
    credits_used: int = 0
    qbo_had: str = "person"          # person | role_only | none  (what QuickBooks alone offered)


# ----------------------------------------------------------------------------- pure helpers
def split_name(name: str | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    name = re.sub(r"\(.*?\)|<.*?>|\"", " ", name).strip()
    if "," in name:                                  # "Zapata, Alejandra"
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    parts = [p for p in name.split() if _NAME_TOKEN.match(p)]
    if not parts or any(p.lower().strip(".") in _NOT_A_NAME for p in parts):
        return None, None            # "Westak Purchasing" is a mailbox label, not a person
    if len(parts) == 1:
        return parts[0].title(), None
    return parts[0].title(), parts[-1].title()


def name_matches_email(first: str | None, last: str | None, email: str) -> bool:
    """'Thomas White' ~ thomas.a.white@ / twhite@ / thomas@. Used to tie a shipping-label name to an address."""
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())
    f, l = (first or "").lower(), re.sub(r"[^a-z]", "", (last or "").lower())
    if not f or not local:
        return False
    if l:
        if local in (f + l, l + f, f[0] + l, l + f[0], f + l[0]):
            return True
        if len(l) >= 4 and l in local and local[0] in (f[0], l[0]):
            return True
    return local == f


def ship_to_names(ship_lines: tuple[str, ...] | list[str], company: str | None,
                  domains: set[str] | None = None) -> list[tuple[str, str | None]]:
    """People named on a shipping label: 'Attn: Paul' -> ('Paul', None); 'Timothy Auman' -> ('Timothy', 'Auman')."""
    comp = normalize_company(company)
    comp_words = set(comp.split())
    roots = {d.split(".")[0] for d in (domains or set())}
    out: list[tuple[str, str | None]] = []

    def consider(text: str, attn: bool) -> None:
        text = re.split(r"[/|,;(]", text)[0].strip()
        tokens = text.split()
        if not tokens or len(tokens) > 3 or not all(_NAME_TOKEN.match(t) for t in tokens):
            return
        low = [t.lower().strip(".") for t in tokens]
        if any(t in _NOT_A_NAME for t in low):
            return
        if any(t.endswith(".") and len(t) > 2 for t in tokens):          # "Az Ic." is an abbreviation, not a name
            return
        # a line that starts with the company name or its web domain names the business, not a person
        # ("Vallen Andover"). A surname that matches the company is fine ("Timothy Auman" at Auman Machine).
        if not attn and (low[0] in comp_words or (len(low[0]) >= 3 and any(low[0] in r for r in roots))):
            return
        if not attn:
            # an unlabeled line must look like "First Last" and must not be the company name
            if len(tokens) < 2 or normalize_company(text) == comp or all(t in comp_words for t in low):
                return
            if not all(t[0].isupper() for t in tokens):
                return
        first, last = split_name(text)
        if first and (first, last) not in out:
            out.append((first, last))

    for i, line in enumerate(ship_lines[:3]):
        m = _ATTN.match(line or "")
        if m:
            consider(m.group(1), attn=True)
        elif i < 2 and not _CARE_OF.match(line or ""):
            consider(line or "", attn=False)
    return out


def looks_like_owner(last_name: str | None, email: str, company: str | None, freemail: set[str],
                     has_company_domain: bool = False) -> bool:
    """One person who orders and uses the product: a personal mailbox at a business with no company email,
    or their surname is the company name."""
    if email_domain(email) in freemail and not has_company_domain:
        return True
    comp_words = set(normalize_company(company).split())
    return bool(last_name) and last_name.lower() in comp_words


def title_role(title: str | None) -> str:
    if not title:
        return "unknown"
    if BUYER_TITLE.search(title):
        return "buyer"
    if OTHER_TITLE.search(title):
        return "unknown"
    if USER_TITLE.search(title):
        return "user"
    return "unknown"


def signature_title(text: str) -> str | None:
    """Best-effort job title from the tail of an email (the signature block). Deliberately strict: a short
    line that names a role, not a sentence, a product name or a company name."""
    tail = text[-700:] if text else ""
    for line in tail.splitlines():
        for part in line.split("|"):
            part = part.strip(" \t,-[]()")
            if not (3 <= len(part) <= 50) or len(part.split()) > 6:
                continue
            if re.search(r"[@:/.!?]|\d{3}", part) or PRODUCT_WORDS.search(part):
                continue
            if BUYER_TITLE.search(part) or USER_TITLE.search(part):
                return part
    return None


def score_inbox(messages: list[MailMessage], domains: set[str], own_domains: set[str], role_patterns: list[str],
                today: date, hint_first_names: set[str] | None = None) -> list[Candidate]:
    """Score everyone at the customer's domain who appears in our mail."""
    people: dict[str, dict] = {}

    def person(email: str, name: str | None) -> dict | None:
        email = (email or "").lower().strip()
        if email_domain(email) not in domains or is_role_address(email, role_patterns):
            return None
        p = people.setdefault(email, {"name": None, "from": 0, "replied": 0, "tech": 0, "po": 0, "title": None,
                                      "last": None})
        if name and not p["name"] and "@" not in name:
            p["name"] = name
        return p

    for m in messages:
        sender_dom = email_domain(m.sender_email)
        if sender_dom in own_domains:
            for name, addr in m.recipients:                 # we wrote to them: a real, attended mailbox
                p = person(addr, name)
                if p:
                    p["replied"] += 1
            continue
        p = person(m.sender_email, m.sender_name)
        if not p:
            continue
        p["from"] += 1
        blob = f"{m.subject}\n{m.text}"
        # judge only what they wrote, not the quoted thread below it
        own = re.split(r"\n\s*(?:From:|-----Original Message|On .{5,80} wrote:)", blob, maxsplit=1)[0]
        if TECH_TALK.search(own):
            p["tech"] += 1
        if PO_TALK.search(own):
            p["po"] += 1
        p["title"] = p["title"] or signature_title(own)
        if m.date and (p["last"] is None or m.date > p["last"]):
            p["last"] = m.date

    out = []
    hints = {h.lower() for h in (hint_first_names or set())}
    for email, p in people.items():
        first, last = split_name(p["name"])
        t_role = title_role(p["title"])
        score, why = 40, []
        if p["tech"]:
            score += 25; why.append(f"asked a technical question ({p['tech']} email{'s' if p['tech'] > 1 else ''})")
        if t_role == "user":
            score += 15; why.append(f"title: {p['title']}")
        if p["po"] or t_role == "buyer":
            score += 15; why.append("sent purchasing emails" if p["po"] else f"title: {p['title']}")
        if p["replied"]:
            score += 10; why.append("we have written to them directly")
        if p["from"] + p["replied"] >= 3:
            score += 5; why.append(f"{p['from'] + p['replied']} emails exchanged")
        if p["last"] and (today - p["last"]).days <= 365:
            score += 5; why.append(f"last emailed {p['last']}")
        if first and first.lower() in hints:
            score += 10; why.append("named on the shipping label")
        # A buyer title or a purchasing mailbox settles it. Otherwise usage questions make a user, unless
        # purchasing emails clearly outnumber them.
        if t_role == "buyer" or PURCHASING_MAILBOX.match(email.split("@", 1)[0]):
            role = "buyer"
        elif t_role == "user" or (p["tech"] and p["tech"] >= p["po"]):
            role = "user"
        elif p["po"]:
            role = "buyer"
        else:
            role = "unknown"
        if not why:
            why.append("appears in our mail")
        out.append(Candidate(email, first, last, p["title"], role, "inbox", min(score, 95), why))
    return out


# ----------------------------------------------------------------------------- resolver
class ContactResolver:
    def __init__(self, db: DB, cfg: Config, world: World, classifier: Classifier, today: date,
                 mail: MailSearch | None = None, people: PeopleLookup | None = None, run_id: int | None = None):
        self.db, self.cfg, self.world, self.clf, self.today = db, cfg, world, classifier, today
        self.mail, self.people, self.run_id = mail, people, run_id
        c = cfg.s("contacts", {}) or {}
        self.enabled = bool(c.get("enabled", True))
        self.threshold = int(c.get("auto_threshold", 65))
        self.cap = int(c.get("monthly_credit_cap", 25))
        self.search_min = float(c.get("apollo_search_min_order", 1000))
        self.search_titles = list(c.get("apollo_search_titles") or
                                  ["process engineer", "manufacturing engineer", "quality engineer",
                                   "production supervisor", "rework technician"])
        self.lookback = int(c.get("inbox_lookback_days", 730))
        self.retry_days = int(c.get("retry_days", 30))
        self.own_domains = {d.lower() for d in (c.get("own_domains") or ["hunterproducts.com", "hunterproducts.co"])}
        self.role_patterns = cfg.s("identity.role_address_patterns", [])
        self.freemail = set(cfg.s("identity.freemail_domains", []))
        self.merge_days = int(cfg.lc("first_order_merge_days", 2))
        self.stats = {"looked_up": 0, "primary_set": 0, "suggested": 0, "no_contact": 0, "credits": 0,
                      "mail_errors": 0}
        self._credits_this_month: int | None = None
        self.last_mail_error: str | None = None

    # ------------------------------------------------------------------ daily entry
    def run(self) -> dict:
        """Look up every first-time buyer in the program who has not been resolved yet."""
        if not self.enabled:
            return self.stats
        for key, acct in self.world.accounts.items():
            fp = first_purchase(self.world, key, self.clf, self.merge_days)
            if fp is None or fp.date < self.cfg.program_start_date or not self._due(key):
                continue
            try:
                res = self.resolve(acct, fp)
                self.commit(res)
            except Exception as exc:      # one bad lookup must not stop the daily run
                log.exception("contact lookup failed for %s", key)
                self.db.log("contact_error", str(exc), "error", self.run_id, account_key=key)
        apply_contacts(self.db, self.world)
        return self.stats

    def _due(self, key: str) -> bool:
        stamp = self.db.get_state(f"contact_lookup:{key}")
        if not stamp:
            return True
        done_on, _, outcome = stamp.partition("|")
        if outcome == "primary":
            return False
        return (self.today - date.fromisoformat(done_on)).days >= self.retry_days   # retry the unresolved monthly

    # ------------------------------------------------------------------ one account
    def resolve(self, acct: Account, fp: FirstPurchase | None, allow_credits: bool = True) -> Resolution:
        cands: dict[str, Candidate] = {}
        would: list[str] = []
        credits = 0
        blocked = self._suppressed()
        domains = {d for d in acct.domains if d not in self.own_domains}
        manual = self._manual_primary(acct.key)

        def add(c: Candidate) -> None:
            e = c.email.lower()
            if e in blocked or is_role_address(e, self.role_patterns) or email_domain(e) in self.own_domains:
                return
            # never leave the customer's own domain, except for addresses QuickBooks itself holds
            if c.source != "qbo" and email_domain(e) not in domains:
                return
            if looks_like_owner(c.last_name, e, acct.company, self.freemail, bool(domains)) \
                    and c.role in ("unknown", "user"):
                c.role = "owner_operator"
                c.evidence.append("personal mailbox or surname matches the company: orders and uses it")
                c.score = min(95, c.score + 15)
            old = cands.get(e)
            if not old:
                c.email = e
                cands[e] = c
                return
            # same person from two sources: keep the better evidence, reward the agreement
            best, other = (c, old) if c.score >= old.score else (old, c)
            best.score = min(95, max(c.score, old.score) + 10)
            best.evidence = list(dict.fromkeys(best.evidence + other.evidence))
            best.first_name, best.last_name = best.first_name or other.first_name, best.last_name or other.last_name
            best.title = best.title or other.title
            if best.role == "unknown":
                best.role = other.role
            # Purchasing evidence beats a shipping-label match: at small companies the box is addressed
            # to the buyer. Only a usage question or a hands-on title keeps them a user.
            ev = " ".join(best.evidence)
            if "buyer" in (c.role, old.role) and best.role == "user" \
                    and "asked a technical question" not in ev and title_role(best.title) != "user":
                best.role = "buyer"
            cands[e] = best

        orders = list(fp.orders) if fp else []
        latest = (self.world.orders.get(acct.key) or [None])[-1]
        if latest and latest not in orders:
            orders.append(latest)
        label_names = []
        for o in orders:
            for n in ship_to_names(o.ship_lines, acct.company, domains):
                if n not in label_names:
                    label_names.append(n)

        # 1. QuickBooks. An address whose owner is also named on the shipping label received the box: a user.
        qbo_people = list(acct.emails)
        for e in qbo_people:
            first, last = self._qbo_name_for(acct, e, single=len(qbo_people) == 1)
            cand = Candidate(e, first, last, None, "unknown", "qbo", 55 + (5 if first else 0),
                             ["on the QuickBooks customer record"])
            if PURCHASING_MAILBOX.match(e.split("@", 1)[0]):
                cand.role, cand.first_name, cand.last_name = "buyer", None, None
                cand.evidence.append("shared purchasing mailbox")
            else:
                for lf, ll in label_names:
                    if name_matches_email(lf, ll, e):
                        cand.role, cand.score = "user", cand.score + 20
                        cand.first_name, cand.last_name = cand.first_name or lf, cand.last_name or ll
                        cand.evidence.append("named on the shipping label")
                        break
            add(cand)
        qbo_had = "person" if qbo_people else ("role_only" if acct.role_emails else "none")
        # label names we already hold an address for need no lookup
        label_names = [n for n in label_names if not any(name_matches_email(n[0], n[1], e) for e in qbo_people)]
        since = self.today - timedelta(days=self.lookback)

        # 2. our own inbox
        if self.mail and domains:
            msgs: list[MailMessage] = []
            for d in sorted(domains)[:3]:
                msgs += self._mail(lambda d=d: self.mail.by_domain(d, since))
            for c in score_inbox(msgs, domains, self.own_domains, self.role_patterns, self.today,
                                 {f for f, _ in label_names}):
                add(c)

        # 3. the purchase order email
        if self.mail and domains:
            for po in {o.po_number for o in orders if o.po_number and len(re.sub(r"\W", "", o.po_number)) >= 5
                       and re.search(r"\d{3}", o.po_number)}:      # a real PO number, not a name typed in the field
                for m in self._mail(lambda po=po: self.mail.by_text(po, since)):
                    if email_domain(m.sender_email) in domains:
                        first, last = split_name(m.sender_name)
                        add(Candidate(m.sender_email, first, last, signature_title(m.text), "buyer", "po_email",
                                      75, [f"sent us PO {po}"]))

        def have_user() -> bool:
            return any(c.score >= self.threshold and ROLE_RANK[c.role] == 0 for c in cands.values())

        # 4. the name on the shipping label, via Apollo (1 credit, only when we still lack a user)
        if not have_user() and not manual:
            for first, last in label_names:
                if not last:
                    continue                      # "Attn: Paul" alone is not enough to look someone up
                for d in sorted(domains)[:1]:
                    if not (self.people and allow_credits and self._credit_left()):
                        would.append(f"{first} {last} @ {d} (shipping label)")
                        continue
                    hit = self.people.match_person(first, last, d, acct.company)
                    email = ((hit or {}).get("email") or "").lower()
                    if hit and email and (hit.get("email_status") or "").lower() == "verified":
                        self._spend(acct.key, f"shipping label name {first} {last}")
                        credits += 1
                        t = hit.get("title")
                        role = title_role(t)
                        add(Candidate(email, first, last, t, role if role != "unknown" else "user", "ship_to",
                                      80 if role == "user" else 70,
                                      ["named on the shipping label", "email verified by Apollo"]
                                      + ([f"title: {t}"] if t else [])))
                if have_user():
                    break

        # 5. Apollo search for an engineer: larger orders only, always goes to review
        total = sum(o.total for o in (fp.orders if fp else []))
        if not manual and not any(c.score >= self.threshold or c.source == "qbo" for c in cands.values()) \
                and total >= self.search_min and domains:
            d = sorted(domains)[0]
            if not (self.people and allow_credits and self._credit_left()):
                would.append(f"engineer search @ {d} (order ${total:,.0f})")
            else:
                for p in self.people.search_people(d, self.search_titles, limit=3)[:1]:
                    hit = self.people.match_person(p.get("first_name") or "", p.get("last_name") or "", d, acct.company)
                    email = ((hit or {}).get("email") or "").lower()
                    if hit and email and (hit.get("email_status") or "").lower() == "verified":
                        self._spend(acct.key, "engineer search")
                        credits += 1
                        add(Candidate(email, hit.get("first_name"), hit.get("last_name"), hit.get("title"),
                                      "user", "apollo_search", 55,
                                      ["found by title search, no prior contact with us", f"title: {hit.get('title')}"]))

        ranked = sorted(cands.values(), key=lambda c: (ROLE_RANK[c.role], -c.score, c.email))
        auto = [c for c in ranked if c.score >= self.threshold or c.source == "qbo"]
        primary = auto[0] if auto else None
        better = [c for c in ranked if c is not primary and c.score < self.threshold
                  and (primary is None or ROLE_RANK[c.role] < ROLE_RANK[primary.role])]
        return Resolution(acct.key, acct.company, primary, better[:3], ranked, would, credits, qbo_had)

    def _qbo_name_for(self, acct: Account, email: str, single: bool) -> tuple[str | None, str | None]:
        first, last = acct.first_name, acct.last_name
        if not first:
            return None, None
        if " " in first.strip():                 # QuickBooks often holds the full name in the first-name field
            f2, l2 = split_name(first)
            first, last = f2, last or l2
        if not first:
            return None, None
        local = email.split("@", 1)[0].lower()
        if single or first.lower() in local or (last and last.lower() in local):
            return first, last
        return None, None

    # ------------------------------------------------------------------ persistence
    def commit(self, res: Resolution) -> None:
        self.stats["looked_up"] += 1
        self.stats["credits"] += res.credits_used
        manual = self._manual_primary(res.account_key)
        now = utcnow()
        with self.db.engine.begin() as c:
            have = {r["email"]: dict(r) for r in c.execute(
                select(contacts).where(contacts.c.account_key == res.account_key)).mappings()}
            for cand in res.candidates[:8]:
                if cand is res.primary and not manual:
                    status = "primary"
                elif cand in res.suggested:
                    status = "suggested"
                else:
                    status = "alternate"
                old = have.get(cand.email)
                if old and old["status"] in ("rejected",) or (old and old["source"] == "manual"):
                    continue                      # a human's decision always stands
                vals = dict(first_name=cand.first_name, last_name=cand.last_name, title=cand.title, role=cand.role,
                            source=cand.source, score=cand.score, evidence=cand.evidence, status=status,
                            updated_at=now)
                if old:
                    c.execute(update(contacts).where(contacts.c.id == old["id"]).values(**vals))
                else:
                    c.execute(insert(contacts).values(account_key=res.account_key, email=cand.email,
                                                      created_at=now, **vals))
            if res.primary and not manual:        # exactly one primary per account
                c.execute(update(contacts).where((contacts.c.account_key == res.account_key)
                                                 & (contacts.c.status == "primary")
                                                 & (contacts.c.email != res.primary.email))
                          .values(status="alternate", updated_at=now))
        outcome = "primary" if (res.primary or manual) else "open"
        self.db.set_state(f"contact_lookup:{res.account_key}", f"{self.today}|{outcome}")
        if res.primary and not manual:
            self.stats["primary_set"] += 1
            self.db.log("contact_found", f"{res.company}: {res.primary.role} via {res.primary.source} "
                        f"(score {res.primary.score})", run_id=self.run_id, account_key=res.account_key,
                        data={"evidence": res.primary.evidence})
        elif not manual:
            self.stats["no_contact"] += 1
        if res.suggested or (res.would_look_up and not res.primary):
            self.stats["suggested"] += 1
            self.db.add_review("contact_suggestion", res.account_key, {
                "company": res.company,
                "current": res.primary.email if res.primary else None,
                "suggested": [{"email": s.email, "name": s.name, "role": s.role, "score": s.score,
                               "why": s.evidence} for s in res.suggested],
                "not_looked_up": res.would_look_up})

    # ------------------------------------------------------------------ helpers
    def _mail(self, fn) -> list[MailMessage]:
        try:
            return fn() or []
        except Exception as exc:              # mailbox trouble degrades to "no inbox evidence", never a crash
            self.stats["mail_errors"] += 1
            self.last_mail_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("inbox search failed: %s", exc)
            return []

    def _suppressed(self) -> set[str]:
        with self.db.engine.begin() as c:
            return {r[0].lower() for r in c.execute(select(suppressions.c.email))}

    def _manual_primary(self, key: str) -> bool:
        with self.db.engine.begin() as c:
            return c.execute(select(contacts.c.id).where(
                (contacts.c.account_key == key) & (contacts.c.status == "primary")
                & (contacts.c.source == "manual"))).first() is not None

    def _credit_left(self) -> bool:
        if self._credits_this_month is None:
            start = datetime(self.today.year, self.today.month, 1)
            with self.db.engine.begin() as c:
                self._credits_this_month = c.execute(select(func.count()).select_from(events).where(
                    (events.c.kind == "apollo_credit") & (events.c.ts >= start))).scalar() or 0
        return self._credits_this_month < self.cap

    def _spend(self, key: str, what: str) -> None:
        self._credits_this_month = (self._credits_this_month or 0) + 1
        self.db.log("apollo_credit", f"1 Apollo credit: {what}", run_id=self.run_id, account_key=key)


def set_manual_contact(db: DB, account_key: str, email: str, role: str = "unknown", first_name: str | None = None,
                       last_name: str | None = None) -> None:
    """A human picked the contact. This always wins over anything the lookup finds later."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    email, now = email.lower().strip(), utcnow()
    with db.engine.begin() as c:
        c.execute(update(contacts).where((contacts.c.account_key == account_key) & (contacts.c.status == "primary"))
                  .values(status="alternate", updated_at=now))
        vals = dict(first_name=first_name, last_name=last_name, role=role, source="manual", score=100,
                    evidence=["chosen by a person"], status="primary", updated_at=now)
        if c.execute(select(contacts.c.id).where((contacts.c.account_key == account_key)
                                                 & (contacts.c.email == email))).first():
            c.execute(update(contacts).where((contacts.c.account_key == account_key) & (contacts.c.email == email))
                      .values(**vals))
        else:
            c.execute(insert(contacts).values(account_key=account_key, email=email, created_at=now, **vals))
    db.set_state(f"contact_lookup:{account_key}", f"{date.today()}|primary")
