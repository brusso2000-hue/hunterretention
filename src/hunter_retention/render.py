"""Renders email copy from content/emails.yaml with real purchase data.

The rendered subject/body are written to Apollo custom fields (hp_subject, hp_body) right before
enrollment; the Apollo sequence step template is just {{hp_subject}} / {{hp_body}}. Copy therefore lives in
version control, and Apollo never holds stale or unapproved text.

Safety: any unresolved "[[VERIFY ...]]" marker or "{{" left in the output blocks a live send.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from jinja2 import Environment, StrictUndefined

from .config import Config

VERIFY_RE = re.compile(r"\[\[\s*VERIFY", re.I)
LEFTOVER_RE = re.compile(r"\{\{|\}\}|\{%|%\}")


@dataclass
class Rendered:
    subject: str
    body: str
    unverified: list[str]
    skip: bool = False               # this contact type does not get this email (e.g. buyers skip technique tips)
    variant: str = "standard"

    @property
    def is_clean(self) -> bool:
        return not self.unverified and not LEFTOVER_RE.search(self.subject + self.body)


class Renderer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                               keep_trailing_newline=False, autoescape=False)
        self.content = cfg.emails

    def context(self, track: dict) -> dict:
        p = self.cfg.products
        fam = track["primary_family"]
        fam_cfg = (p.get("families") or {}).get(fam, {})
        app_labels = p.get("applications") or {}
        lines = (track.get("order_summary") or {}).get("lines", [])
        fam_lines = [ln for ln in lines if ln["family"] == fam]
        fam_lines.sort(key=lambda ln: -(ln.get("amount") or 0))
        main = fam_lines[0] if fam_lines else (lines[0] if lines else {"display": fam_cfg.get("label", "order")})
        consumables, seen = [], set()
        for ln in fam_lines:
            if ln["role"] in ("consumable", "prep") and ln["sku"] not in seen:
                seen.add(ln["sku"])
                consumables.append(ln)
        apps = [a for a in (track.get("applications") or []) if a]
        sender = self.cfg.s("sender", {}) or {}
        first = (track.get("first_name") or "").strip()
        return {
            "first_name": first,
            "greeting": f"Hi {first}," if first else self.content.get("greeting_fallback", "Hello,"),
            "company": track.get("company") or "",
            "segment": track["segment"],
            "family": fam,
            "contact_role": track.get("contact_role") or "unknown",
            "po_number": (track.get("order_summary") or {}).get("po_number") or "",
            "subsegment": track.get("subsegment"),
            "family_label": fam_cfg.get("label", ""),
            "system_url": fam_cfg.get("system_url", ""),
            "store_url": fam_cfg.get("store_url", ""),
            "product": main.get("display"),
            "product_sku": main.get("sku"),
            "product_url": main.get("url") or fam_cfg.get("system_url", ""),
            "products": fam_lines,
            "consumables": consumables,
            "applications": apps,
            "application_labels": [app_labels.get(a, a) for a in apps],
            "first_order_date": track.get("first_order_date"),
            "sender_name": sender.get("name", ""),
            "sender_title": sender.get("title", ""),
            "sender_company": sender.get("company", "Hunter Products"),
            "sender_phone": sender.get("phone", ""),
            "quote_url": self.content.get("links", {}).get("quote", "https://hunterproducts.com/contact/#quote"),
            "tech_url": self.content.get("links", {}).get("technical", ""),
            "resources_url": self.content.get("links", {}).get("resources", ""),
            "guides_url": self.content.get("links", {}).get("guides", ""),
        }

    def render(self, track: dict, touch_key: str) -> Rendered:
        seg = track["segment"]
        tpl = ((self.content.get("tracks") or {}).get(seg) or {}).get(touch_key)
        if not tpl:
            raise KeyError(f"No email content for segment {seg} touch {touch_key}")
        # Role-specific copy: a version on the email itself wins, then the shared role_variants, then standard.
        role = track.get("contact_role") or "unknown"
        var = (tpl.get("variants") or {}).get(role)
        if var is None:
            var = ((self.content.get("role_variants") or {}).get(role) or {}).get(touch_key)
        variant = "standard"
        if var:
            if var.get("skip"):
                return Rendered("", "", [], skip=True, variant=role)
            tpl = {**tpl, **var}
            variant = role
        ctx = self.context(track)
        # application blocks (Micro-Metallizer day-30 tips, etc.)
        blocks_cfg = ((self.content.get("blocks") or {}).get(seg) or {}).get(touch_key) or {}
        blocks = []
        keys = ctx["applications"] or ["general"]
        if seg == "A" and len(keys) > 1:
            keys = keys[: int(self.content.get("max_application_blocks", 2))]
        if seg != "A":
            keys = [track.get("subsegment") or "general"]
        for k in keys:
            src = blocks_cfg.get(k) or blocks_cfg.get("general")
            if src:
                blocks.append(self.env.from_string(src).render(**ctx).strip())
        ctx["blocks"] = blocks
        subject = self.env.from_string(tpl["subject"]).render(**ctx).strip()
        body = self.env.from_string(tpl["body"]).render(**ctx)
        body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
        unverified = VERIFY_RE.findall(subject + body)
        markers = re.findall(r"\[\[\s*VERIFY[^\]]*\]\]", subject + body, flags=re.I)
        return Rendered(subject, body, markers or unverified, variant=variant)


def body_to_html(body: str) -> str:
    """Optional: for Apollo setups where a plain-text custom field loses its line breaks."""
    paras = [p for p in body.strip().split("\n\n") if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paras)
