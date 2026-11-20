"""Customer identity resolution.

QBO often has several records for one buyer (re-created customers, sub-customers/jobs, plant-level
records). We collapse them into accounts so purchase history is checked across all of them:

  * sub-customers / jobs roll up to their top-level parent
  * records sharing any email address are the same account
  * records with the same normalized company name AND the same non-freemail email domain are the same account

Account key = "acct:<lowest QBO root id in the group>", stable as groups only grow.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import email_domain, is_role_address, normalize_company, parse_emails


@dataclass
class Account:
    key: str
    customer_ids: list[str]
    root_ids: list[str]
    company: str | None
    first_name: str | None
    last_name: str | None
    emails: list[str] = field(default_factory=list)          # personal (non-role) addresses, best first
    role_emails: list[str] = field(default_factory=list)
    domains: set[str] = field(default_factory=set)
    contact_role: str = "unknown"            # user | buyer | owner_operator | unknown
    contact_source: str = "qbo"

    @property
    def contact_email(self) -> str | None:
        return self.emails[0] if self.emails else None


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # keep the numerically lowest id as representative for stability
            lo, hi = sorted([ra, rb], key=_id_sort)
            self.p[hi] = lo


def _id_sort(x: str):
    return (0, int(x)) if x.isdigit() else (1, x)


def resolve_accounts(customers: list[dict], role_patterns: list[str], freemail: set[str],
                     extra_emails: dict[str, list[str]] | None = None) -> tuple[dict[str, Account], dict[str, str]]:
    """customers: rows from qbo_customers (deleted ones excluded by caller).
    extra_emails: customer_id -> emails seen on their invoices (BillEmail), used as a fallback.
    Returns (accounts by key, customer_id -> account key)."""
    by_id = {c["id"]: c for c in customers}

    def root_of(cid: str) -> str:
        seen = set()
        cur = cid
        while by_id.get(cur, {}).get("parent_id") and cur not in seen:
            seen.add(cur)
            parent = by_id[cur]["parent_id"]
            if parent not in by_id:
                break
            cur = parent
        return cur

    roots = {cid: root_of(cid) for cid in by_id}
    uf = _UF()
    email_owner: dict[str, str] = {}
    company_domain_owner: dict[tuple[str, str], str] = {}
    emails_by_root: dict[str, list[str]] = {}

    for cid, c in by_id.items():
        r = roots[cid]
        uf.find(r)
        ems = parse_emails(c.get("email_raw")) + [e for e in (extra_emails or {}).get(cid, [])]
        lst = emails_by_root.setdefault(r, [])
        for e in ems:
            if e not in lst:
                lst.append(e)

    for r, ems in emails_by_root.items():
        comp = normalize_company(by_id[r].get("company_name") or by_id[r].get("display_name"))
        for e in ems:
            if e in email_owner:
                uf.union(email_owner[e], r)
            else:
                email_owner[e] = r
            dom = email_domain(e)
            if comp and dom and dom not in freemail:
                k = (comp, dom)
                if k in company_domain_owner:
                    uf.union(company_domain_owner[k], r)
                else:
                    company_domain_owner[k] = r

    groups: dict[str, list[str]] = {}
    for cid in by_id:
        groups.setdefault(uf.find(roots[cid]), []).append(cid)

    accounts: dict[str, Account] = {}
    cust_to_acct: dict[str, str] = {}
    for rep, cids in groups.items():
        key = f"acct:{rep}"
        root_ids = sorted({roots[c] for c in cids}, key=_id_sort)
        rep_row = by_id[rep]
        # prefer a record that actually has a person's given name
        named = next((by_id[c] for c in sorted(cids, key=_id_sort) if by_id[c].get("given_name")), None)
        personal, role = [], []
        for r in root_ids + [c for c in cids if c not in root_ids]:
            for e in emails_by_root.get(roots[r], []) + parse_emails(by_id[r].get("email_raw")):
                bucket = role if is_role_address(e, role_patterns) else personal
                if e not in bucket:
                    bucket.append(e)
        acct = Account(
            key=key, customer_ids=sorted(cids, key=_id_sort), root_ids=root_ids,
            company=rep_row.get("company_name") or rep_row.get("display_name"),
            first_name=(named or {}).get("given_name"), last_name=(named or {}).get("family_name"),
            emails=personal, role_emails=role,
            domains={d for d in (email_domain(e) for e in personal + role) if d and d not in freemail},
        )
        accounts[key] = acct
        for c in cids:
            cust_to_acct[c] = key
    return accounts, cust_to_acct
