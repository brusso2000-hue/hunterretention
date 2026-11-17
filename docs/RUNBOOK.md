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
