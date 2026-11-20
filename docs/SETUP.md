# Setup and go-live

About 2 hours of one-time work. Nothing reaches a customer until step 9.

## 1. Repository and database
1. Create a **private** GitHub repo (e.g. `hunter-retention`) and push this folder.
2. Create a free Postgres database (Neon or Supabase). Copy the connection string.
3. In the repo: Settings > Secrets and variables > Actions > add secret `DATABASE_URL`.

## 2. QuickBooks access
1. developer.intuit.com > Dashboard > Create an app > QuickBooks Online and Payments. Scope: `com.intuit.quickbooks.accounting`.
2. Production keys require Intuit's short app-details questionnaire (it asks for privacy/EULA URLs; `https://hunterproducts.com/privacy/` works for a private internal app).
3. Use the **OAuth 2.0 Playground** (developer.intuit.com > Tools) with your production keys. Connect to HUNTER PRODUCTS INC., then copy the **Realm ID** and **Refresh token**.
4. Add secrets `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REALM_ID`.
5. Store the refresh token once (it rotates automatically after this):
   ```bash
   pip install ".[postgres,crypto]"
   export DATABASE_URL=...   # same as the secret
   hunter-retention set-qbo-token <refresh_token>
   ```
   Optional: generate `HP_TOKEN_KEY` (see `.env.example`) and add it as a secret **before** this step to encrypt the token at rest.

## 3. Apollo API key and custom fields
1. Apollo > Settings > Integrations > API > Create new key > enable **Set as master key**. Add as secret `APOLLO_API_KEY`.
2. Create the 15 contact fields (`quickbooks_customer_id` ... `hp_subject`, `hp_body`):
   ```bash
   export APOLLO_API_KEY=...
   hunter-retention setup-apollo --create
   ```
   `hp_body` must be a **multi-line text** field. If the API refuses that type, the command says so; change the field type in Apollo > Settings > Fields.

## 4. Four one-step sequences in Apollo
Create four sequences, identical except the name:

| Name | Step 1 |
|---|---|
| HP Lifecycle e1 (setup) | Automatic email, wait 0 days |
| HP Lifecycle e2 (tips) | same |
| HP Lifecycle e3 (replenish) | same |
| HP Lifecycle e4 (check-in) | same |

Each step: subject `{{hp_subject}}`, body `{{hp_body}}` (insert both through the `{ }` custom-variable menu). No signature (the body has one). Settings: send from **hunter@hunterproducts.com**, your existing weekday schedule, "mark finished if reply" on, open tracking off (it hurts deliverability and opens are unreliable anyway). Approve/activate each one.

Copy the four sequence ids (from the URL `.../sequences/<id>`) into `config/settings.yaml` under `apollo.sequences` and commit.

## 5. First run: backfill in dry-run
`config/settings.yaml` ships with `mode: dry_run`. Run the workflow manually (Actions > hunter-retention-daily > Run workflow) or locally:
```bash
hunter-retention run
hunter-retention audit-items     # every QBO item and how it's classified
hunter-retention review          # anything that needs a decision
```
Download the `retention-report` artifact and open `latest.html`.

To see who would be enrolled right now, temporarily set `program_start_date` 60 days back and run again. Each `dry_run_send` event has the exact rendered subject and body.

## 6. Finish the copy
```bash
hunter-retention content-lint
hunter-retention preview --segment A --app gold --touch e2
```
Replace every `[[VERIFY: ...]]` in `content/emails.yaml` with approved wording. Until you do, the emails containing them will not send in live mode (e1 and e4 for every product line have none and can go live first).

## 7. Sandbox test (real Apollo, internal inbox only)
Set `mode: sandbox` (or run the workflow with mode = sandbox). Up to 5 real renders per run are sent to `sandbox.redirect_to` with `[TEST segment/touch company]` in the subject. Check:
* line breaks look right (if paragraphs run together, set `apollo.body_format: html`)
* links work, the sender is hunter@hunterproducts.com, no extra signature
Then remove the sandbox contact from the four sequences in Apollo.

## 8. Choose the go-live date
Set `program_start_date` to the go-live date (or up to ~30 days earlier to catch recent buyers; touches whose window has closed are skipped automatically). Decide `analytics.holdout_pct` (10 by default; 0 emails everyone).

## 9. Approve production
```bash
hunter-retention approve-production --by "Brandon" --confirm "SEND REAL EMAILS"
```
Then in GitHub: Settings > Secrets and variables > Actions > **Variables** > `HP_ALLOW_LIVE_SEND` = `true`, and set `mode: live` in `config/settings.yaml`. All three are required.

## 10. Alerts (optional but recommended)
Add `ALERT_WEBHOOK_URL` (Teams channel > Connectors > Incoming Webhook). Without it you still get GitHub's failed-workflow email.

## Optional: Outlook search for the contact lookup

Lets the daily job see who at a customer has emailed hunter@ or sales@. Read-only.

1. Microsoft Entra admin center > App registrations > New registration. Name it "Hunter retention mail read".
2. API permissions > Add > Microsoft Graph > Application permissions > `Mail.Read` > Grant admin consent.
3. Certificates & secrets > New client secret. Copy the value.
4. Add three GitHub repository secrets: `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`.
5. Recommended: restrict the app to the two mailboxes in `contacts.mailboxes` with an application access policy.

Without these secrets the lookup skips the inbox and PO-email steps and still uses QuickBooks and Apollo.
