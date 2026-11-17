# Hunter Products first-time customer retention system

QuickBooks Online → Python job → Apollo. Finds first-time buyers every day, puts them on the right product track (Micro-Metallizer, Carbitron 300, Micro-Jet 200, consumables-only), sends four well-timed emails from hunter@hunterproducts.com, and stops reminders as soon as QuickBooks shows a reorder.

* **How it works / why:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* **One-time setup and go-live:** [docs/SETUP.md](docs/SETUP.md)
* **Monitoring and troubleshooting:** [docs/RUNBOOK.md](docs/RUNBOOK.md)
* **Email copy, rendered:** [docs/EMAIL_SEQUENCES.md](docs/EMAIL_SEQUENCES.md) (source: `content/emails.yaml`)

## What you edit
| File | What |
|---|---|
| `config/products.yaml` | SKU → product line / role / metal. Built from your live QBO item list. |
| `config/lifecycle.yaml` | Segments, timing, reorder suppression rules |
| `config/settings.yaml` | Mode, go-live date, Apollo ids, sender, caps, holdout |
| `content/emails.yaml` | All email copy |

## Commands
```
hunter-retention run [--today YYYY-MM-DD] [--full-sync] [--skip-sync]
hunter-retention audit-items [--all]          # classification of every QBO item
hunter-retention review [--resolve ID --action enroll|exclude|done]
hunter-retention preview --segment A --app gold --touch e2
hunter-retention content-lint                 # lists [[VERIFY]] markers left
hunter-retention suppress add|remove EMAIL [--support-hold]
hunter-retention setup-apollo [--create]
hunter-retention set-qbo-token TOKEN
hunter-retention approve-production --by NAME --confirm "SEND REAL EMAILS"
hunter-retention revoke-production
hunter-retention report
```

## Tests
```
pip install ".[dev]" && pytest -q
```
Scenario tests drive the real daily job day by day against fake QuickBooks and Apollo: first-time buyer full lifecycle, reorder before Day 60, unrelated purchase, mixed orders, duplicate/sub-customer records, domain matches, back-dated imports, voids, deletes, refunds, $0 samples, missing/AP emails, unsubscribes, replies, support holds, frequency caps, a 40-day outage, a QuickBooks outage, Apollo transient and permanent failures, crash recovery, the Day-120 live recheck, dry-run/sandbox/live gates and unverified-copy blocking.
