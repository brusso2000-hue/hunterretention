"""Product classification engine.

Line level:  QBO item -> (status, family, role, applications)
Order level: classified lines -> segment A/B/C/D (or E = unclassified) using a configurable priority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import normalize_item_name

LIFECYCLE = "lifecycle"
NON_PRODUCT = "non_product"
IGNORED = "ignored"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ItemClass:
    status: str
    family: str | None = None
    role: str | None = None
    applications: tuple[str, ...] = ()
    display: str | None = None
    url: str | None = None
    sku: str | None = None


@dataclass
class Line:
    item_id: str | None
    item_name: str | None
    description: str | None
    qty: float
    amount: float
    cls: ItemClass


@dataclass
class OrderClass:
    segment: str | None                      # A B C D, or None when nothing classifiable (segment E)
    subsegment: str | None
    primary_family: str | None
    families: list[str] = field(default_factory=list)
    applications: list[str] = field(default_factory=list)
    lifecycle_lines: list[Line] = field(default_factory=list)
    unclassified_lines: list[Line] = field(default_factory=list)
    secondary_segments: list[str] = field(default_factory=list)

    @property
    def is_unclassified(self) -> bool:
        return self.segment is None


class Classifier:
    def __init__(self, products_cfg: dict, lifecycle_cfg: dict):
        self.cfg = products_cfg
        self.by_id: dict[str, dict] = {}
        self.by_name: dict[str, dict] = {}
        self.regexes: list[tuple[re.Pattern, dict]] = []
        for entry in products_cfg.get("items", []) or []:
            if entry.get("qbo_item_id"):
                self.by_id[str(entry["qbo_item_id"])] = entry
            if entry.get("name"):
                self.by_name[normalize_item_name(entry["name"])] = entry
            if entry.get("name_regex"):
                self.regexes.append((re.compile(entry["name_regex"], re.I), entry))
        np = products_cfg.get("non_product", {}) or {}
        self.non_product_names = {normalize_item_name(n) for n in np.get("names", [])}
        self.non_product_rx = [re.compile(p, re.I) for p in np.get("name_regex", [])]
        self.ignored_names = {normalize_item_name(n) for n in (products_cfg.get("ignored", {}) or {}).get("names", [])}
        self.priority: list[str] = lifecycle_cfg.get("segment_priority", ["B", "C", "A", "D"])
        self.family_segment: dict[str, str] = lifecycle_cfg.get(
            "family_segment", {"micro_metallizer": "A", "carbitron": "B", "micro_jet": "C"})
        # roles that put a family's buyer into its own segment (otherwise -> consumables-only segment D)
        self.segment_roles: dict[str, list[str]] = lifecycle_cfg.get("segment_roles", {
            "micro_metallizer": ["equipment", "consumable"],
            "carbitron": ["equipment"],
            "micro_jet": ["equipment"],
        })

    # ------------------------------------------------------------------ items
    def classify_item(self, item_id: str | None, item_name: str | None) -> ItemClass:
        norm = normalize_item_name(item_name)
        entry = None
        if item_id and str(item_id) in self.by_id:
            entry = self.by_id[str(item_id)]
        elif norm in self.by_name:
            entry = self.by_name[norm]
        else:
            for rx, e in self.regexes:
                if rx.match(norm):
                    entry = e
                    break
        if entry is not None:
            apps = entry.get("applications") or ([entry["application"]] if entry.get("application") else [])
            return ItemClass(LIFECYCLE, entry["family"], entry["role"], tuple(apps),
                             entry.get("display") or item_name, entry.get("url"), norm or None)
        if not norm or norm in self.non_product_names or any(rx.match(norm) for rx in self.non_product_rx):
            return ItemClass(NON_PRODUCT, sku=norm or None)
        if norm in self.ignored_names:
            return ItemClass(IGNORED, sku=norm)
        return ItemClass(UNCLASSIFIED, display=item_name, sku=norm)

    # ------------------------------------------------------------------ orders
    def classify_lines(self, lines: list[Line]) -> OrderClass:
        lifecycle = [ln for ln in lines if ln.cls.status == LIFECYCLE]
        unclassified = [ln for ln in lines if ln.cls.status == UNCLASSIFIED]
        families: list[str] = []
        for ln in lifecycle:
            if ln.cls.family not in families:
                families.append(ln.cls.family)

        candidates: dict[str, str] = {}   # segment -> family
        for fam in families:
            fam_roles = {ln.cls.role for ln in lifecycle if ln.cls.family == fam}
            seg = self.family_segment.get(fam)
            if seg and fam_roles & set(self.segment_roles.get(fam, [])):
                candidates.setdefault(seg, fam)
            else:
                candidates.setdefault("D", fam)

        if not candidates:
            return OrderClass(None, None, None, families, [], lifecycle, unclassified)

        ordered = [s for s in self.priority if s in candidates]
        segment = ordered[0]
        primary_family = candidates[segment]
        apps: list[str] = []
        for ln in lifecycle:
            if ln.cls.family == primary_family:
                for a in ln.cls.applications:
                    if a not in apps:
                        apps.append(a)
        sub = self._subsegment(segment, primary_family, apps)
        return OrderClass(segment, sub, primary_family, families, apps, lifecycle, unclassified,
                          secondary_segments=ordered[1:])

    @staticmethod
    def _subsegment(segment: str, family: str, apps: list[str]) -> str:
        if segment == "A":
            if len(apps) == 0:
                return "general"          # kit / power supply only; application unknown, do not guess
            if len(apps) == 1:
                return apps[0]
            return "multi"
        if segment == "D":
            return family
        return family
