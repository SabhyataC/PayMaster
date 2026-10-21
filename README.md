# Paymaster

Paymaster is an accounts-payable agent that pays a company's vendor invoices automatically and safely. Think of it as a careful accounts clerk that never forgets to pay and never pays twice. Built for the PayPal AI Hackathon.

> **All data is SYNTHETIC and everything runs in the PayPal sandbox (fake money).** No real company, person or bank account is represented.

## How the Pay stage works

1. **Policy check** (`backend/policy.py`): an approved invoice is checked against rules (amount limit, new payee). The answer is pass, hold (a human must release it) or block.
2. **Reserve the payment** (`backend/pay.py`, `backend/idempotency.py`): the payment is saved before PayPal is contacted, with a unique key per invoice, so the same invoice always gets the same key.
3. **Send the money** (`backend/paypal_client.py`): PayPal Payouts is called with that key. Answers are sorted into submitted, duplicate, rejected or unknown, and a retry never pays twice.
4. **Wait for PayPal's confirmation** (`backend/webhooks.py`): PayPal sends webhooks, and each one's signature is verified before it is trusted.
5. **Update the books** (`backend/settle.py`): the invoice is marked paid and balanced double-entry ledger rows are written for the payment and the fee. Every decision is recorded in the audit log.

## Status

- Done: PayPal sandbox tests, Postgres database, synthetic company, Pay stage, webhooks, ledger, audit log.
- Next: Verify (invoice intake, investigator agent, fraud detection), Reconcile, console.

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

1. **Approve the invoice.** Until the Verify stage exists, approval is done by hand:

```powershell
docker exec -i paymaster-db psql -U paymaster -d paymaster -c "UPDATE invoices SET status='approved' WHERE invoice_number='INV-1001';"
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