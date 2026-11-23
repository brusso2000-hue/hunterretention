# Runbook: monitoring and troubleshooting

## Daily
Nothing. The job runs at 6:41am ET. You hear about it only when:
* a run fails (GitHub email + webhook alert)
* a first-time order contains unmapped products
* an Apollo enrollment fails 3 times
* live sending is blocked (unverified copy, missing approval)

## Weekly (2 minutes)
* Actions > latest run > artifact > `latest.html`
* `hunter-retention review` and clear the queue

## Common tasks

| Task | How |
|---|---|
| New product | Add a line to `config/products.yaml` (`name` = QBO item name). Commit. Customers held as unclassified enroll on the next run if their window is still open. |
| Change timing | `config/lifecycle.yaml` (`offset_days`, `window_days`, replenishment min/max). Affects newly scheduled touches. |
| Change copy | `content/emails.yaml`, then `hunter-retention preview ...` |
| Customer has an open support issue | `hunter-retention suppress add jane@x.com --support-hold --reason "RMA 1234"`; `suppress remove` when resolved |
| Never email someone | `hunter-retention suppress add jane@x.com --reason "asked"` |
| Domain-match / AP-email hold | `hunter-retention review`, then `review --resolve <id> --action enroll [--email person@x.com]` or `--action exclude` |
| Stop all sending now | `hunter-retention revoke-production` (or set variable `HP_ALLOW_LIVE_SEND` to false). Runs keep syncing and planning. |
| Re-sync everything | Run workflow with "full sync", or `hunter-retention run --full-sync` |

## Troubleshooting

**"No QBO refresh token stored" / 401 from Intuit.** The token chain broke (revoked app, 100+ days idle, or the 5-year cap). Re-run the OAuth Playground and `set-qbo-token`.

**Run failed: QuickBooks unavailable.** Nothing was sent. The next run catches up. If the outage exceeded 29 days the next run does a full re-sync automatically.

**Touch shows `expired`.** It couldn't go out inside its window (system down, record held, or product mapped late). By design: no stale emails.

**Touch deferred `active_in_other_apollo_sequence`.** The customer is in a cold or reactivation sequence. It retries daily until that finishes or the window closes.

**Touch deferred `content_unverified`.** That email still has a `[[VERIFY]]` marker. `hunter-retention content-lint`.

**Touch deferred `engagement_sync_failed`.** Apollo status couldn't be read, so nothing was sent blind. Usually transient. If it persists, check the API key is still a master key.

**First sandbox send: verify engagement fields.** Apollo's message-search payload isn't fully documented. After the first sandbox email is delivered and replied to, check `engagement.raw` in the database and confirm `replied`/`delivered` were detected. `message_flags()` in `apollo.py` accepts the common field-name variants.

**Duplicate Apollo contacts.** Contacts are found by exact email before creating, and created with `run_dedupe`. Merge any old duplicates in Apollo; the job will reuse the surviving record.

**Wrong segment.** Fix the mapping. If no email has been sent yet, the track rebuilds automatically next run. If emails already went out, it stops and appears in `review` (`first_order_changed_after_send`).

## Event log
Every decision is in the `events` table: `track_created`, `enrolled`, `reorder_detected`, `touch_expired`, `track_canceled`, `dry_run_send` (with full rendered copy), `live_recheck_suppressed`, errors.
```sql
select ts, kind, account_key, message from events order by id desc limit 50;
```

## Contact lookup (who gets the email)

QuickBooks often holds an accounts-payable inbox, not the person using the product. Before planning, the
daily job looks up each new first-time buyer once and stores the answer in the `contacts` table.

Order tried: QuickBooks emails, our Outlook history for the customer's domain, the sender of the PO email,
the shipping-label name via Apollo (1 credit), then an Apollo engineer search for first orders of $1,000+
(always sent to review). Settings are under `contacts:` in `config/settings.yaml`.

Each contact is tagged `user`, `buyer`, `owner_operator` or `unknown`. The tag picks the email version
(`role_variants:` in `content/emails.yaml`). Buyers skip the technique email.

| Task | Command |
|---|---|
| See who it would pick for recent buyers (writes nothing, 0 credits) | `hunter-retention contacts preview --days 730` |
| List stored contacts | `hunter-retention contacts list` |
| Choose a contact by hand (always wins) | `hunter-retention contacts set --account acct:123 --email a@b.com --role user` |
| Accept a suggestion from the review list | `hunter-retention review --resolve N --action enroll --email a@b.com --role user` |
| Read an email as a given contact type | `hunter-retention preview --segment A --app gold --touch e3 --role buyer` |

Safety rules: role inboxes are never emailed, found addresses must be on the customer's own domain,
Apollo emails must be verified, suppressed addresses are skipped, Apollo lookups run only in the modes in
`contacts.credit_modes` and stop at `contacts.monthly_credit_cap` per month. Email text is read for scoring
and never stored.

## Things that changed at go-live (Nov 23, 2026)

- **Live.** Mode is `live`, approval is recorded, `HP_ALLOW_LIVE_SEND=true` is set in GitHub. To pause: switch the
  "HP Lifecycle" sequences off in Apollo, or run the workflow with input `revoke-production`.
- **Send cap.** 5 a day until Dec 8, 2026, then 25 a day. Both are in `config/settings.yaml` under `dispatch`.
- **Sequence pool.** Apollo sends a contact each sequence only once. There are 15 identical one-step sequences
  (`apollo.sequences` plus `apollo.sequence_pool`); every email uses one the person has not been through. The job
  warns when someone has 3 or fewer left. Fix: create more one-step sequences with subject `{{contact.hp_subject}}`
  and body `{{contact.hp_body}}`, activate them, add the ids to `apollo.sequence_pool`.
- **Catch-all domains.** Apollo refuses to send to some domains (Settings > Outbound > Sending policies). The job
  marks those emails failed and raises an alert. Fix: add the customer's domain to the allowlist on that page.
- **Alerts.** A failed run, or any alert, opens a GitHub issue that mentions the owner.
- **Unsubscribe footer.** Turned off in Apollo (Settings > Personal > Profile > Email settings) at the owner's request.
- **Test sends.** Workflow input `test-send:smoke:text` sends one sample to `sandbox.redirect_to` only.
