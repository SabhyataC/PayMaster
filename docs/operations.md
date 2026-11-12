# Operations guide

Everything operational that used to live in the README: setup details, configuration, evaluation commands, reconcile, alerts, webhooks and paying an invoice. The main [README](../README.md) has the overview.


## How it works

Paymaster runs every invoice through four stages. Every decision is written to the audit log with the reason.

1. **Intake** (`backend/intake.py`): Gemini reads the invoice (PDF, scan or email) into structured fields. Anything below a 0.8 confidence goes to `needs_review` instead of the invoice table.
2. **Verify**: three layers, and the AI can never be looser than the code.
   - **Hard checks** (`backend/checks.py`): PO exists and belongs to this vendor, delivery received, amount within 2% of the PO, duplicate invoice, bank details changed.
   - **Fraud signals** (`backend/fraud.py`): bank changed recently or differs, amount near the auto-pay limit, new payee, sender email domain mismatch, look-alike vendor name, scam wording in the email. Each signal is low, medium or high; a high total risk can never end as approve.
   - **Investigator agent** (`backend/investigator.py`): a Gemini agent with read-only tools investigates and gives a verdict. Code gates then enforce the rules: only a hard check can reject (the AI can only escalate to hold), and a held invoice can never be moved back to approved by Verify.
3. **Review** (`backend/review.py`, `backend/review_api.py`): the only way a held invoice becomes approved is a named person with a written reason. A person can never approve a duplicate, a missing PO or another vendor's PO. Two people cannot both decide the same invoice.
4. **Pay**:
   - **Policy check** (`backend/policy.py`): amount limit, new payee. The answer is pass, hold (a human must release it) or block.
   - **Reserve the payment** (`backend/pay.py`, `backend/idempotency.py`): saved before PayPal is contacted, with a unique key per invoice, so a retry never pays twice.
   - **Send the money** (`backend/paypal_client.py`): PayPal Payouts is called with that key.
   - **Wait for PayPal's confirmation** (`backend/webhooks.py`): each webhook's signature is verified before it is trusted.
   - **Update the books** (`backend/settle.py`): the invoice is marked paid and balanced double-entry ledger rows are written for the payment and the fee.

## Status

- Done: PayPal sandbox, Postgres, synthetic company, Intake, Verify (hard checks, fraud signals, investigator), labeled evaluation, human review flow and API, case memory, Pay stage, webhooks, ledger, audit log, Reconcile (settlements, exceptions, AI explainer), AG Grid console (invoices, review queue, payments and ledger, exceptions, audit timeline, summary cards), chat, cash advice, upload with content checks, vendors and POs with deliveries and attachments, change approvals, team and settings pages.
- Next: end-to-end test, demo video.
- Results and limits: see `docs/ai-results.md` and `docs/fraud-signals.md`.

## PayPal sandbox accounts

- The **Business** account (REST app `paymaster-us`) is the company. It sends the payouts and must be US-based, because an India-based sender gets `403 PAYOUT_NOT_AVAILABLE`.
- The **Personal** accounts `paymaster-vendor-us`, `-us2`, `-in`, `-gm` and `-uk` (all `@example.com`) are the vendors. They receive the payouts.
- PayPal's dashboard calls Personal accounts "buyers" and Business accounts "sellers". That only describes the account type, not the direction of money in Paymaster.

## Setup

Requirements: Python 3.11+, Docker Desktop, git, a PayPal developer account with sandbox access, and `cloudflared` (or any tunnel) to receive webhooks.

Clone the repo and install the dependencies (PowerShell):

```powershell
git clone https://github.com/SabhyataC/PayMaster.git
cd PayMaster
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On macOS or Linux, activate with `source .venv/bin/activate` instead.

## Configuration

1. Copy the example file and fill in your own values:

```powershell
Copy-Item .env.example .env
```

2. In the PayPal developer dashboard (Sandbox), open the REST app `paymaster-us` and copy its client ID and secret into `.env`. This must be the **US Business** account's app. An India-based sender gets `403 PAYOUT_NOT_AVAILABLE`.
3. Choose a password in `.env` (`POSTGRES_PASSWORD`, and the same one inside `DATABASE_URL`).
4. `PAYPAL_WEBHOOK_ID` is filled in later, in the Webhooks step below.


`.env` is gitignored. Never commit it.

| Variable | Needed | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | yes | Postgres connection string |
| `JWT_SECRET` | yes | At least 32 random characters. The API refuses to log anyone in without it. Changing it logs everyone out |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | yes | Used by Intake, Verify, the explainer and chat |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_BASE_URL` | yes | PayPal sandbox app |
| `PAYPAL_WEBHOOK_ID` | for webhooks | With no value every webhook is refused (503) |
| `PAYPAL_ACCOUNT_EMAIL` | no | Shown read-only in the console |
| `CONSOLE_ORIGINS` | yes when deployed | Comma-separated exact origins allowed by CORS. `*` is refused. Default is the local console |
| `SIGNUP_ENABLED` | no | Default `false`: public signup is closed and admins add people on the Team page. Set `true` to allow public signup |
| `TRUST_PROXY` | no | `true` only behind a proxy you control, so login limits use the real client address |
| `COOKIE_SAMESITE` | no | `lax` (default), `strict` or `none` (needs https) |

If a real key or secret was ever pasted into a chat, a file or a commit, rotate it at its provider. Rotating cannot be done from code. `.env` is gitignored; no secrets are committed.

## Database

Start Postgres in Docker (host port 5433), then create the tables:

```powershell
docker compose up -d
Get-Content db\schema.sql | docker exec -i paymaster-db psql -U paymaster -d paymaster
```

`db/schema.sql` can be re-run safely.

## Seed the synthetic company

The default database is named `paymaster`, which the seed script refuses unless you allow it on purpose:

```powershell
$env:ALLOW_SEED = "1"
python -m backend.seed
```

This **erases all tables first** and rebuilds the demo company: 10 SYNTHETIC vendors, 8 purchase orders with deliveries, and 10 invoices. Five invoices are clean. Five contain planted problems for the Verify stage to catch: a changed bank email, a duplicate invoice, an invoice with no PO, and a look-alike vendor name.

Five vendors use real sandbox account emails (`paymaster-vendor-*@example.com`), so their payouts reach SUCCESS. The planted-problem vendors use fake emails, because they should never reach PayPal.

## Host it on Render

One web service serves both the API and the console, plus one Postgres database. Both come from `render.yaml`.

1. Push the repo to GitHub. In Render choose **New > Blueprint** and pick the repo. Render builds the `Dockerfile` and creates `paymaster` and `paymaster-db`.
2. Fill in the secrets Render asks for (everything marked `sync: false` in `render.yaml`): `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `GEMINI_API_KEY`, and optionally `PAYPAL_ACCOUNT_EMAIL`, the `SMTP_*` and `TWILIO_*` values and `CONSOLE_URL` (`https://<service>.onrender.com/console`). `JWT_SECRET` is generated for you. `DATABASE_URL` is wired automatically.
3. The container applies `db/schema.sql` on every start (safe to repeat), then starts the server on Render's `$PORT`.
4. Open `https://<service>.onrender.com/console/login.html`. The console talks to the same address it is served from, so no CORS or URL settings are needed.
5. In the PayPal developer dashboard set the webhook URL to `https://<service>.onrender.com/webhooks/paypal`, copy the new webhook id into `PAYPAL_WEBHOOK_ID`, and redeploy. No tunnel is needed any more.
6. Judges sign up (`SIGNUP_ENABLED=true` in `render.yaml`), create a company, and press **Add demo data** on the Team page (`ALLOW_DEMO_DATA=1`). Remove both settings on a real deployment.

Notes: the free web plan sleeps after 15 minutes idle and the first request then takes about a minute, so use the Starter plan while judging or open the site a few minutes before. Free Postgres expires after 30 days. If Gemini cannot be reached, invoices are held for a person instead of approved, which is deliberate. Render's free plan has no shell, which is why the demo data is a button and not a script.

## Demo data button (for judges)

Set `ALLOW_DEMO_DATA=1` in the server's environment. Then an admin sees an **Add demo data** card on the Team page. One click adds five synthetic vendors and five invoices to **that admin's own company**: a clean invoice, changed bank details, billed before delivery, a copy of an earlier invoice, and one over the automatic payment limit. Verify then runs on them in the background. It never deletes anything, never touches another company, refuses a second click (409), and is written to the audit log. Without `ALLOW_DEMO_DATA=1` the card is hidden and the endpoint answers 403, so a real deployment cannot be filled with fake invoices by accident.

The clean vendors pay out to the PayPal sandbox accounts, so approving and paying the clean invoice reaches SUCCESS. The over-limit invoice is approved by Verify and held for a person when you press Pay.

## Review held invoices

Start the server (it serves the webhooks and the review API):

```powershell
uvicorn backend.webhooks:app --port 8000
```

Every call needs a logged-in member of the company. Deciding and paying need the reviewer or admin role; viewers can only read.

```powershell
$login = Invoke-RestMethod -Method Post http://localhost:8000/auth/login -ContentType "application/json" -Body (@{email="you@example.com"; password="your password"} | ConvertTo-Json)
$h = @{ Authorization = "Bearer $($login.token)" }
# what needs a person
Invoke-RestMethod http://localhost:8000/review/queue -Headers $h
# one invoice with its evidence, fraud signals and history
Invoke-RestMethod http://localhost:8000/review/<invoice_id> -Headers $h
# approve or reject (a name and a note of at least 10 characters are required)
Invoke-RestMethod -Method Post http://localhost:8000/review/<invoice_id>/decision -Headers $h -ContentType "application/json" -Body '{"decision":"approve","note":"Called vendor, bank change confirmed","confirmed_with_vendor":true}'
# send the payment; naming yourself here releases a policy hold such as a new payee
Invoke-RestMethod -Method Post http://localhost:8000/review/<invoice_id>/pay -Headers $h
```

The queue has two kinds of item. `verify_hold` means Verify wants a person (decide with `/decision`). `policy_hold` means the invoice is already approved but the pay policy wants a person (release with `/pay`).

## Evaluation (synthetic data)

`scripts/make_test_set.py` builds 56 SYNTHETIC invoices (28 clean, 28 bad) with the correct decision for each in `samples/eval_set.csv`, and sets 10 aside as a hold-out. `scripts/eval.py` runs Verify on them and prints frauds caught, clean invoices wrongly held and bad invoices wrongly approved. Use a **separate database** whose name contains `eval` or `test`: both scripts erase data, and refuse to run on any other name.

```powershell
docker exec -i paymaster-db createdb -U paymaster paymaster_eval
$env:DATABASE_URL = "postgresql://paymaster:<password>@localhost:5433/paymaster_eval"
python -m scripts.make_test_set
python -m scripts.eval --stub-ai        # code layers only, no Gemini needed
python -m scripts.eval --sleep 15       # real Gemini (slow on the free tier)
python -m scripts.eval --holdout        # hold-out set: once, at the very end
python -m scripts.adversarial_eval --stub-ai   # 60 more unseen invoices (quantity, split billing, duplicate spellings...), code only
python -m scripts.adversarial_eval --sleep 8 --out adv_real.csv   # same on the REAL model: publish these numbers
```

Results, what failed along the way and the limits of this test are in `docs/ai-results.md`.

### More than precision and recall

`scripts/eval.py` also prints three further tables (and writes them with `--json metrics.json`; definitions in `backend/evalmetrics.py`, hand-checked in `tests/test_evalmetrics.py`):

- **Investigation accuracy:** share of invoices decided right (bad stopped, clean approved), the exact hold-vs-reject match, the AI alone before the code gates versus the whole pipeline, and how many bad invoices the code gates rescued from the AI.
- **Evidence correctness:** was the invoice stopped for the RIGHT reason (for a duplicate, `duplicate_invoice`), and were clean invoices shown any false evidence? Low fraud signals such as a new payee are context, not counted.
- **Confidence calibration:** invoices grouped by the confidence the verdict reported, against how often the decision was right, with expected calibration error, Brier score and the count of wrong decisions made at 90% or more confidence.

Code-only dev run (`--stub-ai`, 46 synthetic invoices): decision accuracy 100%; the stubbed AI alone 54.3% and the code gates rescued 21 bad invoices from it; right reason on 21 of 21 stopped bad invoices; 2 of 25 clean invoices were shown a medium `amount_near_limit` signal (the two near-limit invoices, expected). The stub reports confidence 1.0 for everything, so the calibration table needs the real AI to mean anything. Fill it in from `python -m scripts.eval --sleep 15 --json metrics.json`.

### Results (synthetic, self-written data)

Positive = "this invoice is bad and must not be paid". A false negative is a bad invoice approved (the dangerous kind). A false positive is a clean invoice held for a person to look at (costs time, not money). All invoices are SYNTHETIC and written by the authors, so these numbers show the gates work on cases we thought of. They are not a claim about real-world accuracy.

| Run | Invoices | Precision | Recall | False positives | False negatives |
|---|---|---|---|---|---|
| Dev set, code only (`--stub-ai`, AI replaced by a stub that always approves) | 46 | 100.0% | 100.0% | 0 | 0 |
| Adversarial set, code only (`--stub-ai`, seed 2026) | 60 | 100.0% | 100.0% | 0 | 0 |
| Dev set, real Gemini | 46 | _fill in from `python -m scripts.eval --sleep 15`_ | | | |
| Adversarial set, real Gemini | 60 | _fill in from `python -m scripts.adversarial_eval --sleep 8`_ | | | |

The code-only rows are the important ones: they show that even when the AI is wrong or hostile, the hard checks and fraud gates stop every bad invoice in these sets. Each run prints its own row in this format.



## Fix documents that need review

When Intake is not sure about a document (low confidence, unknown vendor, unknown PO, duplicate number) it is saved as `needs_review` and no invoice is created. A person fixes the fields:

```powershell
(Invoke-RestMethod -Uri http://localhost:8000/extractions -Headers $h).items
$body = @{note="Vendor name had a typo, checked against the PO"; corrections=@{vendor_name="Bluefin Packaging"}} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://localhost:8000/extractions/<ID>/correct -Headers $h -ContentType "application/json" -Body $body
```

- Only `needs_review` extractions can be corrected (anything else returns 409). A reviewer name and a note of at least 10 characters are required.
- The first AI-extracted values are kept in `original_extracted` and never overwritten. Each correction writes an audit row with original and corrected value per field.
- The fields the person fixed count as checked, but the code checks still run on the new values (totals must match line items, dates in order, vendor and PO must exist). A wrong correction goes back to `needs_review` with the reason.
- If it passes, the invoice is created as `received` and Verify runs again on it. Whether it may be paid is still decided by Verify, the review flow and policy, never by the correction.

## Reconcile

Reconcile compares our payment records with PayPal and logs every difference as an **exception**. Payouts younger than 3 hours are ignored, because PayPal's Transaction Search can lag.

Run it (the server is `uvicorn backend.webhooks:app --port 8000`; reviewer or admin role, and it only looks at your own company's payments):

```powershell
$login = Invoke-RestMethod -Method Post http://localhost:8000/auth/login -ContentType "application/json" -Body (@{email="you@example.com"; password="your password"} | ConvertTo-Json)
$h = @{ Authorization = "Bearer $($login.token)" }
Invoke-RestMethod -Method Post -Uri http://localhost:8000/reconcile/run -Headers $h
```

Running it twice in a row does not create duplicate exceptions. Each run writes an audit row.

Other endpoints (same token): `GET /exceptions`, `GET /exceptions/{id}`, `POST /exceptions/{id}/explain`, `POST /exceptions/{id}/resolve` (needs `resolved_by` and `note`; writes an audit row and moves no money).

| Exception type | What it means |
|---|---|
| `unknown_outcome` | We sent a payout but never learned the result (for example a timeout), still unresolved after 30 minutes. |
| `pending_too_long` | The payout has been submitted or pending for more than 1 hour. |
| `unclaimed_too_long` | The vendor has not claimed the payout after 7 days. Reconcile cancels it, waits for PayPal to mark it RETURNED, writes reversing ledger rows (old rows are never edited) and makes the invoice payable again. |
| `success_mismatch` | We recorded the payment as successful but PayPal says otherwise. |
| `amount_mismatch` | The amount or currency PayPal shows differs from our settlement record. |
| `unknown_payout` | PayPal shows a payout that we have no payment record for. |
| `payment_failed` | PayPal refused the payment. A person can retry it. |
| `created_too_long` | A payment was saved but never sent, still so after 15 minutes. |

**AI explainer.** `POST /exceptions/{id}/explain` returns a plain-English explanation and one suggested action from a fixed list (`wait`, `cancel_unclaimed`, `contact_vendor`, `recheck_with_paypal`, `escalate`; anything else becomes `escalate`). The AI only explains and suggests: it never pays or cancels. If Gemini fails, a fixed template for the exception type is used. Scores and limits are in `docs/ai-results.md`.


## Security and privacy

- Every company's data is separate. Each table carries a required `company_id`, the database refuses a row that points at another company's vendor, purchase order, invoice or payment, and `company_id` can never be changed after a row exists. Every lookup the AI investigator can make is fixed by code to the invoice's own company; the AI cannot choose or change it.
- Roles: viewer (read), reviewer (upload, decide, release payments, resolve exceptions), admin (also vendors, POs, people, settings). A company always keeps one active admin.
- Passwords are bcrypt hashed. Logins are limited per email and per address in the database, so a restart does not reset the limit. Logging out, changing a password, a reset or a role change ends every older session. There is no email service, so an admin creates a one-time reset token, shown once and valid for an hour.
- Uploads are checked by content, not name: real PDF, PNG, JPG or UTF-8 text; at most 10 MB and 20 PDF pages; no scripts, launch actions or embedded files in PDFs; limited per person and per company.
- The AI only reads. Anything inside an invoice, email, vendor name or reviewer note is treated as data. Code, never the AI, decides what may be paid: only hard checks can reject, only a named person can release a hold, and a hostile AI that approves everything still cannot get a bad invoice paid (`python -m scripts.eval --stub-ai`, `python tests/test_hardening.py`).
- Privacy: this project is a demo on synthetic data. Documents you upload are sent to Google's Gemini API for reading, and are stored unencrypted in your Postgres database. Do not upload real personal or financial data unless you accept that. It is not certified or audited and makes no claim of compliance with PCI DSS, SOC 2, GDPR or any other standard.

## Safety rules for payments

- A held invoice is released only by a named person with a written reason (at least 10 characters). Four-eyes separation (uploader cannot approve) was removed so a one-person demo company can complete the whole flow.
- Payouts always go to the vendor's payout email on file, never to an email printed on an invoice.
- Changing a vendor's payout email needs a second admin to approve it. If a company has only one admin, the change applies at once and the audit log records a WARNING.
- The auto-pay limit cannot be set above 50,000.
- A payment PayPal refuses opens a `payment_failed` exception and can be retried by a person. A payment saved but never sent opens a `created_too_long` exception.

## Known limitations (demo)

- One shared PayPal sandbox account sends every company's payouts. A real product would give each company its own funding account.
- Signup does not verify email addresses. This is why signup is off by default.
- New vendors, POs, deliveries and auto-pay limit changes need only one admin.

## Limits

- Currencies: USD, GBP and EUR only. Amounts above the company's automatic payment limit (default 5000.00, set on the Team page) are always held for a person.
- Quantity: checked only when the PO and the invoice both state a quantity (the invoice quantity is the sum of its lines, and is used only if every line has one). It may not exceed what was received (or ordered, if no received quantity is recorded) minus what earlier invoices on the same PO already billed. If either side has no quantity the check says "not checked" and the amount checks still apply.
- The 2% amount tolerance and the 0.8 confidence threshold are tunable defaults.
- Tax is not stored, only the invoice total. Invoices are read in English only.
- Seeding: `python -m backend.seed` erases every table. It refuses unless the database name contains `dev`, `demo`, `test` or `eval`, or you set `ALLOW_SEED=1` on purpose.
- Extraction quality depends on the AI model; see `docs/ai-results.md`. If the AI is unavailable the invoice is held, never approved.

## Alerts by email and WhatsApp

An admin opens **Team > Alerts**, adds up to 5 email addresses and 5 WhatsApp numbers, picks which events to hear about (invoice on hold, payment problem, document could not be read, payment confirmed) and clicks **Send a test message**.

- Alerts are written to an outbox in the same transaction as the event, so a change that is rolled back never sends anything. A background worker sends them, retries a failed send up to 3 times and records every attempt in the table on the Alerts card.
- Messages contain only our own wording, the invoice number, the vendor name (cleaned), the amount and a short reason. They never contain bank details or text from an invoice.
- A failing email or WhatsApp provider can never block Verify or a payment.
- Server settings live in `.env` (see `.env.example`): `SMTP_*` for email and `TWILIO_*` for WhatsApp. The WhatsApp sandbox only reaches phones that first sent the join code to the sandbox number; a production sender needs Twilio approval and message templates.
- Alerts are one way. You cannot approve or pay by replying to a message.

## Investigation report

Open an invoice's Trust graph page: below the chain it shows an investigation report from `GET /report/invoice/{id}`. Every claim says what kind of basis it has (code check, fraud signal, AI opinion, code overruled the AI, a person's decision) and lists the record ids it rests on. It also lists each lookup the investigator made and the earlier human cases (case memory) it found. Text written by people or the AI is shown as data, never as instructions. Case memory can inform a reviewer, it can never approve anything.

## Payment reliability, anomaly signals and new pages

- **Retries:** PayPal calls retry on timeouts, 408, 429 and 5xx (3 tries, 1s then 2s backoff plus up to 0.5s jitter, `PAYPAL_MAX_TRIES`). A 4xx is permanent and never retried. Every try uses the same `PayPal-Request-Id`, so PayPal can never pay twice.
- **Unknown outcome:** if every try times out the payment is `unknown`. A background job (every 60s, and inside every reconcile run) re-sends with the same key only if the invoice is still `approved`; otherwise a person is alerted. Set `RECOVER_WORKER=0` to disable the loop.
- **Vendor behaviour signals:** amount far above the vendor's normal (needs 3+ past invoices) and invoice bursts within 24 hours raise fraud risk.
- **Pages:** `approvals.html` (approval history with overrides), `reconcile.html` (books vs PayPal status, run reconcile), and a payment lifecycle strip on the Trust Graph page (`GET /lifecycle/{ref}`).
- **Tests:** `python -m tests.test_payment_reliability`, `test_anomaly`, `test_insights`, and `test_lifecycle_e2e` (invoice to reconcile through the real code, plus timeout and failure variants).
- **Concurrency across processes:** `python -m tests.test_concurrent_processes` starts separate OS processes that release the same invoice at the same instant against a fake PayPal over real HTTP (eight at once, three invoices in parallel, pay vs reject, and PayPal answering 503 mid-race). PayPal must see exactly one payout per invoice.
- **Real sandbox check:** `SANDBOX_RECEIVER_EMAIL=... python -m scripts.sandbox_smoke` sends one real sandbox payout and prints the Trust Graph verdict.

## Audit log integrity

`audit_log` is append-only: database triggers refuse `UPDATE`, `DELETE` and `TRUNCATE`. A background sealer (every 5 s, `AUDIT_SEAL_WORKER=0` disables it) links each company's rows into a SHA-256 hash chain, and `GET /audit/chain/verify` (and the **Audit integrity** page) recomputes the whole chain: an edited, removed, reordered or forged row is detected, including by someone with full database access. Writers never wait for each other. Details, the recommended separate runtime role and what this does not prove: `docs/audit-integrity.md`. Run `python -m tests.test_audit_chain`.

## Trust Graph

Every invoice and payment has an evidence chain: Vendor, Invoice, PO, Delivery, Approval, Payout, PayPal webhook, Ledger, Reconcile. Open it from the "Trust graph" link on an invoice, a review-queue row or a payment (`console/trust.html`, API `GET /trust/invoice/{ref}` and `GET /trust/payment/{id}`). Each link is green (verified), red (missing or mismatch, with the reason in words) or grey (not reached yet), lists the record ids behind it, and is tagged "Verified fact" (our database, checked by code) or "AI suggestion" (read or suggested by Gemini). It only reads recorded data; it never asks an AI anything and never changes anything. A successful payout with no webhook after 10 minutes is shown red.

## When the AI is slow or down

Every Gemini call waits 2 s and 5 s between tries, then switches to `GEMINI_FALLBACK_MODEL` (if set). If both fail the upload is marked failed (nothing is approved) and the Needs attention card shows a **Retry** button. A held invoice whose PO or delivery arrives later is re-checked automatically when that PO or delivery is saved, and has a **Re-verify** button. Re-verify runs the same full checks; it never pays.

## Console

A web console for the review queue, invoices, payments and ledger, exceptions, the audit timeline and uploads. Static pages, no build step (AG Grid from a CDN): `index.html` (invoices, review, payments, exceptions, audit, upload), `vendors.html` (vendors and POs), `team.html`, `account.html`, `company.html`, `paypal.html`, `login.html`, plus `pm.js`.

**The demo runs locally.** The API runs on port 8000 and the console on port 5500, the only origin the API's CORS setting allows.

```powershell
uvicorn backend.webhooks:app --port 8000        # terminal 1
cd console; python -m http.server 5500          # terminal 2
```

Open http://localhost:5500 and log in. The session is an HttpOnly cookie: page scripts never see it, and it is sent with an `X-CSRF` header on every change. Set `CONSOLE_ORIGINS` in `.env` to the exact origin(s) the console is served from; `*` is refused. Viewers do not see buttons they cannot use, and the server refuses those calls anyway. Admins manage people, roles and the automatic payment limit on the Team page; everyone can change their own password on the My account page.

Read-only endpoints behind the console (any logged-in member of the company): `GET /invoices`, `GET /payments`, `GET /payments/{id}` (settlement, ledger rows and a balanced check), `GET /ledger`, `GET /audit/{invoice_id}` (full timeline; the invoice number also works), `GET /summary`.

Other endpoints (same login): `POST /upload`, `/vendors`, `/pos`, `/deliveries` (vendors, purchase orders, deliveries, attachments), `POST /chat`, `GET /advise`, `GET /attention`, `/changes` (approve or reject a pending change) and `/admin` (people and settings, admin only).


## Webhooks

PayPal confirms each payout by calling a public URL, so a local server needs a tunnel.

1. Start a tunnel to port 8000 and note the public URL it prints:

```powershell
cloudflared tunnel --url http://localhost:8000
```

A free quick tunnel gets a **new URL every time it restarts**, so you must update the webhook URL in PayPal each time. A named tunnel on your own domain keeps one stable URL.

2. In the PayPal dashboard, open the REST app `paymaster-us`, go to Webhooks, and add or edit a webhook:
   - URL: `https://<your-tunnel-host>/webhooks/paypal`
   - Event types: All Events
3. Copy the **Webhook ID** into `.env` as `PAYPAL_WEBHOOK_ID`.
4. Start the webhook server (in a second terminal, from the repo root):

```powershell
uvicorn backend.webhooks:app --port 8000
```

Every incoming webhook's signature is verified with PayPal before it is trusted. A fake request gets `400`.


## Pay an invoice

Make sure the webhook server and the tunnel are running (see Webhooks above), or PayPal's confirmation will never reach you.

1. **Verify the invoice.** The investigator decides approve, hold or reject:

```powershell
python -m backend.investigator INV-1001
```

Or run all ten seeded invoices and see one summary line each: `python -m scripts.run_investigator`.

2. **Try to pay it.** A payee we have never paid before is **held** for a human:

```powershell
python -m scripts.pay_invoice INV-1001
```

Result: `{'status': 'held', 'reasons': ['new_payee']}`

3. **Release the hold** as a named human. This sends the payout to PayPal:

```powershell
python -m scripts.pay_invoice INV-1001 owner
```

Result: `status: submitted`, with a PayPal batch ID.

4. **Wait a few minutes.** PayPal sends webhooks. The webhook terminal should show `VERIFIED` and `SYNC` lines, and the payment moves to `success`.

Running the pay command again never pays twice. An invoice that already has a payment returns `already_exists`, and a paid invoice is `blocked`.

> **Do not pay the planted-problem invoices by hand:** INV-6001, INV-7001, inv-7001, INV-8001 and INV-9001. They exist for the Verify stage to catch.

### Check the result

Payment status:

```powershell
docker exec -i paymaster-db psql -U paymaster -d paymaster -c "SELECT i.invoice_number, i.status AS invoice_status, p.status AS payment_status, p.paypal_batch_id FROM invoices i JOIN payments p ON p.invoice_id = i.id;"
```

Ledger (a balanced debit and credit for the payment, and the same for PayPal's fee):

```powershell
docker exec -i paymaster-db psql -U paymaster -d paymaster -c "SELECT entry_type, account, direction, amount, currency FROM ledger_entries ORDER BY created_at;"
```

Audit log (every decision, with the reason):

```powershell
docker exec -i paymaster-db psql -U paymaster -d paymaster -c "SELECT created_at, stage, decision, reason FROM audit_log ORDER BY created_at;"
```

## More docs

- `docs/reconcile.md`, `docs/ai-results.md`, `docs/fraud-signals.md`
- `sandbox-findings.md`: PayPal sandbox notes
- Tests (use a test database): `python -m tests.test_bugfixes`, `python tests/test_hardening.py`
- License: MIT (see `LICENSE`)