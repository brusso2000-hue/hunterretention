"""Regenerate docs/EMAIL_SEQUENCES.md from content/emails.yaml with sample purchases.
Usage: python scripts/render_email_doc.py"""
from pathlib import Path

from hunter_retention.cli import sample_track
from hunter_retention.config import ROOT, load_config
from hunter_retention.render import Renderer

TITLES = {"e1": "Email 1: setup check-in", "e2": "Email 2: day-30 tips", "e3": "Email 3: replenishment (day 60 to 90)",
          "e4": "Email 4: day-120 personal check-in (only if no reorder)"}
SECTIONS = [("A", "gold", "Micro-Metallizer (shown for a gold buyer: Contact Repair Kit + 24K gold pens)"),
            ("B", None, "Carbitron 300"), ("C", None, "Micro-Jet 200"),
            ("D", "carbitron", "Consumables / parts only (shown for an electrode-only buyer)")]

cfg = load_config()
r = Renderer(cfg)
out = ["# Email sequences (rendered)", "",
       "Generated from `content/emails.yaml` with a sample customer (Jordan at Sample Circuits). "
       "Real sends use the customer's own name, products, part numbers and links.",
       "Lines with **[[VERIFY: ...]]** need your approved wording; those emails cannot go out live until replaced.", ""]
for seg, app, title in SECTIONS:
    out += [f"## {title}", ""]
    for t in cfg.lc(f"tracks.{seg}.touches"):
        tc = t.get("offset_days", "60-90 (replenishment)")
        rend = r.render(sample_track(cfg, seg, app), t["key"])
        out += [f"### {TITLES[t['key']]}", f"_Timing: day {tc} after estimated delivery. "
                f"Suppressed by: {', '.join(t.get('suppress_on') or ['nothing'])}._", "",
                f"**Subject:** {rend.subject}", "", "```", rend.body.rstrip(), "```", ""]
out += ["## Micro-Metallizer day-30 tip blocks by metal", "",
        "Email 2 inserts the block(s) for the metals the customer bought (max 2):", ""]
for k, v in cfg.emails["blocks"]["A"]["e2"].items():
    out += [f"**{k}**", "```", v.rstrip(), "```", ""]
Path(ROOT / "docs" / "EMAIL_SEQUENCES.md").write_text("\n".join(out))
print("wrote docs/EMAIL_SEQUENCES.md")
