from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc|lp|llp|sa|ag|bv)\b\.?",
    re.I,
)


def parse_emails(raw: str | None) -> list[str]:
    """QBO allows several addresses in one field separated by commas/semicolons."""
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        e = part.strip().strip("<>").lower()
        if e and EMAIL_RE.match(e) and e not in out:
            out.append(e)
    return out


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].lower()


def is_role_address(email: str, patterns: list[str]) -> bool:
    local = email.split("@", 1)[0].lower()
    for p in patterns:
        if re.fullmatch(p, local):
            return True
    return False


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\(.*?\)", " ", s)          # drop "(plant 2)" style qualifiers
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def normalize_item_name(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"\s*\(deleted\)\s*$", "", name.strip(), flags=re.I)
    # QBO sub-items come through as "Parent:Child"; classify on the leaf
    if ":" in s:
        s = s.split(":")[-1]
    return " ".join(s.split()).upper()


def parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def stable_bucket(key: str, buckets: int = 100) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % buckets

