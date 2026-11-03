# Paymaster

Paymaster is an accounts-payable agent that pays a company's vendor invoices automatically and safely. Think of it as a careful accounts clerk that never forgets to pay and never pays twice. Built for the PayPal AI Hackathon.

> **All data is SYNTHETIC and everything runs in the PayPal sandbox (fake money).** No real company, person or bank account is represented.

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

- Done: PayPal sandbox, Postgres, synthetic company, Intake, Verify (hard checks, fraud signals, investigator), labeled evaluation, human review flow and API, case memory, Pay stage, webhooks, ledger, audit log, Reconcile (settlements, exceptions, AI explainer), AG Grid console (invoices, review queue, payments and ledger, exceptions, audit timeline, summary cards).
- Next: light cash-advice panel, end-to-end test, demo video.
- Results and limits: see `docs/ai-results.md` and `docs/fraud-signals.md`.

## PayPal sandbox accounts

- The **Business** account (REST app `paymaster-us`) is the company. It sends the payouts and must be US-based, because an India-based sender gets `403 PAYOUT_NOT_AVAILABLE`.
- The **Personal** accounts `paymaster-vendor-us`, `-us2`, `-in`, `-gm` and `-uk` (all `@example.com`) are the vendors. They receive the payouts.
- PayPal's dashboard calls Personal accounts "buyers" and Business accounts "sellers". That only describes the account type, not the direction of money in Paymaster.

## Setup

Requirements: Python 3.X, Docker Desktop, git, a PayPal developer account with sandbox access, and `cloudflared` (or any tunnel) to receive webhooks.

1. Clone the repo and install the dependencies (PowerShell):

```powershell
git clone https://github.com/<your-username>/paymaster.git
cd paymaster
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
| `SIGNUP_ENABLED` | no | `false` closes public signup; admins then add people on the Team page |
| `TRUST_PROXY` | no | `true` only behind a proxy you control, so login limits use the real client address |
| `COOKIE_SAMESITE` | no | `lax` (default), `strict` or `none` (needs https) |

If a real key or secret was ever pasted into a chat, a file or a commit, rotate it at its provider. Rotating cannot be done from code. A scan of the full git history found no secrets.

## Database

Start Postgres in Docker (host port 5433), then create the tables:

```powershell
docker compose up -d
Get-Content db\schema.sql | docker exec -i paymaster-db psql -U paymaster -d paymaster
```

`db/schema.sql` can be re-run safely.

## Seed the synthetic company

```powershell
python -m backend.seed
```

This **erases all tables first** and rebuilds the demo company: 10 SYNTHETIC vendors, 8 purchase orders with deliveries, and 10 invoices. Five invoices are clean. Five contain planted problems for the Verify stage to catch: a changed bank email, a duplicate invoice, an invoice with no PO, and a look-alike vendor name.

Five vendors use real sandbox account emails (`paymaster-vendor-*@example.com`), so their payouts reach SUCCESS. The planted-problem vendors use fake emails, because they should never reach PayPal.

## Review held invoices

Start the server (it serves the webhooks and the review API):

```powershell
uvicorn backend.webhooks:app --port 8000
```

Every call needs a logged-in member of the company. Deciding and paying need the reviewer or admin role; viewers can only read. The person who uploaded an invoice cannot approve or release it when another reviewer or admin exists.

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
createdb paymaster_eval
$env:DATABASE_URL = "postgresql://<user>:<password>@localhost:<port>/paymaster_eval"
python -m scripts.make_test_set
python -m scripts.eval --stub-ai        # code layers only, no Gemini needed
python -m scripts.eval --sleep 15       # real Gemini (slow on the free tier)
python -m scripts.eval --holdout        # hold-out set: once, at the very end
python -m scripts.adversarial_eval --stub-ai   # 60 more unseen invoices (quantity, split billing, duplicate spellings...), code only
python -m scripts.adversarial_eval --sleep 8 --out adv_real.csv   # same on the REAL model: publish these numbers
```

Results, what failed along the way and the limits of this test are in `docs/ai-results.md`.


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

**AI explainer.** `POST /exceptions/{id}/explain` returns a plain-English explanation and one suggested action from a fixed list (`wait`, `cancel_unclaimed`, `contact_vendor`, `recheck_with_paypal`, `escalate`; anything else becomes `escalate`). The AI only explains and suggests: it never pays or cancels. If Gemini fails, a fixed template for the exception type is used. Scores and limits are in `docs/ai-results.md`.


## Security and privacy

- Every company's data is separate. Each table carries a required `company_id`, the database refuses a row that points at another company's vendor, purchase order, invoice or payment, and `company_id` can never be changed after a row exists. Every lookup the AI investigator can make is fixed by code to the invoice's own company; the AI cannot choose or change it.
- Roles: viewer (read), reviewer (upload, decide, release payments, resolve exceptions), admin (also vendors, POs, people, settings). The person who uploaded an invoice cannot approve or release it when another reviewer or admin exists. A company always keeps one active admin.
- Passwords are bcrypt hashed. Logins are limited per email and per address in the database, so a restart does not reset the limit. Logging out, changing a password, a reset or a role change ends every older session. There is no email service, so an admin creates a one-time reset token, shown once and valid for an hour.
- Uploads are checked by content, not name: real PDF, PNG, JPG or UTF-8 text; at most 10 MB and 20 PDF pages; no scripts, launch actions or embedded files in PDFs; limited per person and per company.
- The AI only reads. Anything inside an invoice, email, vendor name or reviewer note is treated as data. Code, never the AI, decides what may be paid: only hard checks can reject, only a named person can release a hold, and a hostile AI that approves everything still cannot get a bad invoice paid (`python -m scripts.eval --stub-ai`, `python tests/test_hardening.py`).
- Privacy: this project is a demo on synthetic data. Documents you upload are sent to Google's Gemini API for reading, and are stored unencrypted in your Postgres database. Do not upload real personal or financial data unless you accept that. It is not certified or audited and makes no claim of compliance with PCI DSS, SOC 2, GDPR or any other standard.

## Limits

- Currencies: USD, GBP and EUR only. Amounts above the company's automatic payment limit (default 5000.00, set on the Team page) are always held for a person.
- Quantity: checked only when the PO and the invoice both state a quantity (the invoice quantity is the sum of its lines, and is used only if every line has one). It may not exceed what was received (or ordered, if no received quantity is recorded) minus what earlier invoices on the same PO already billed. If either side has no quantity the check says "not checked" and the amount checks still apply.
- Tax is not stored, only the invoice total. Invoices are read in English only.
- Seeding: `python -m backend.seed` erases every table. It refuses unless the database name contains `dev`, `demo`, `test` or `eval`, or you set `ALLOW_SEED=1` on purpose.
- One shared PayPal business account sends every company's payouts.
- Extraction quality depends on the AI model; see `docs/ai-results.md`. If the AI is unavailable the invoice is held, never approved.

## When the AI is slow or down

Every Gemini call waits 2 s and 5 s between tries, then switches to `GEMINI_FALLBACK_MODEL` (if set). If both fail the upload is marked failed (nothing is approved) and the Needs attention card shows a **Retry** button. A held invoice whose PO or delivery arrives later is re-checked automatically when that PO or delivery is saved, and has a **Re-verify** button. Re-verify runs the same full checks; it never pays.

## Console

A read-mostly web console for the review queue, invoices, payments and ledger, exceptions and the audit timeline. It is one static file (`console/index.html`, AG Grid from a CDN), no build step.

**The demo runs locally.** The API runs on port 8000 and the console on port 5500, the only origin the API's CORS setting allows.

```powershell
uvicorn backend.webhooks:app --port 8000        # terminal 1
cd console; python -m http.server 5500          # terminal 2
```

Open http://localhost:5500 and log in. The session is an HttpOnly cookie: page scripts never see it, and it is sent with an `X-CSRF` header on every change. Set `CONSOLE_ORIGINS` in `.env` to the exact origin(s) the console is served from; `*` is refused. Viewers do not see buttons they cannot use, and the server refuses those calls anyway. Admins manage people, roles and the automatic payment limit on the Team page; everyone can change their own password on the My account page.

Read-only endpoints behind the console (any logged-in member of the company): `GET /invoices`, `GET /payments`, `GET /payments/{id}` (settlement, ledger rows and a balanced check), `GET /ledger`, `GET /audit/{invoice_id}` (full timeline; the invoice number also works), `GET /summary`.


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