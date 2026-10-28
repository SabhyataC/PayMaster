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

- Done: PayPal sandbox, Postgres, synthetic company, Intake, Verify (hard checks, fraud signals, investigator), labeled evaluation, human review flow and API, case memory, Pay stage, webhooks, ledger, audit log, Reconcile (settlements, exceptions, AI explainer), first AG Grid console.
- Next: console polish (invoices, payments, audit timeline), cash advice, demo video.
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

Every call needs the header `X-Review-Token` with the value from `.env`.

```powershell
$h = @{ "X-Review-Token" = "<your REVIEW_TOKEN>" }
# what needs a person
Invoke-RestMethod http://localhost:8000/review/queue -Headers $h
# one invoice with its evidence, fraud signals and history
Invoke-RestMethod http://localhost:8000/review/<invoice_id> -Headers $h
# approve or reject (a name and a note of at least 10 characters are required)
Invoke-RestMethod -Method Post http://localhost:8000/review/<invoice_id>/decision -Headers $h -ContentType "application/json" -Body '{"decision":"approve","reviewer":"Your Name","note":"Called vendor, bank change confirmed"}'
# send the payment; naming yourself here releases a policy hold such as a new payee
Invoke-RestMethod -Method Post http://localhost:8000/review/<invoice_id>/pay -Headers $h -ContentType "application/json" -Body '{"reviewer":"Your Name"}'
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
```

Results, what failed along the way and the limits of this test are in `docs/ai-results.md`.

## Reconcile

Reconcile compares our payment records with PayPal and logs every difference as an **exception**. Payouts younger than 3 hours are ignored, because PayPal's Transaction Search can lag.

Run it (the server is `uvicorn backend.webhooks:app --port 8000`; the token is `REVIEW_TOKEN` from `.env`):

```powershell
$tok = ((Select-String -Path .env -Pattern '^REVIEW_TOKEN=').Line -replace '^REVIEW_TOKEN=','') -replace '"',''
Invoke-RestMethod -Method Post -Uri http://localhost:8000/reconcile/run -Headers @{"X-Review-Token"=$tok}
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
```

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