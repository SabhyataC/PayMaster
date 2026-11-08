# Devpost write-up (draft)

## The problem
Small finance teams pay invoices from email. One changed bank detail or a convincing fake invoice means the money is gone, and AI tools that read invoices can be talked into approving them.

## What it does
Paymaster reads invoices (Gemini intake), checks them against purchase orders, deliveries, vendor bank history and duplicates, scores fraud signals, and lets an investigator agent look things up. A person reviews anything doubtful. Approved invoices are paid through the PayPal sandbox Payouts API; webhooks, a double-entry ledger and a reconcile job confirm the money moved the way we said it would.

## The idea: the AI is never trusted with the money
The AI can only suggest. Code gates can overrule it (never the other way), humans release payments, and every decision is written to an audit log. The Trust graph and investigation report show, for any invoice, which claim rests on which record.

## How PayPal is used
Payouts API for payments with idempotency keys, webhooks for payout results, sandbox settlement and fees in the ledger, reconcile against PayPal batch data.

## How AI is used
Gemini extracts invoice fields, runs an investigator with read-only tools (PO, deliveries, bank history, similar invoices, case memory), and helps explain results. Its output is untrusted input to the code gates.

## Results (synthetic, self-written data)
See the README table: with the AI replaced by a hostile stub, 0 bad invoices approved on 106 synthetic invoices. Real-Gemini numbers: <fill in>.

## Honest limits
Synthetic test data written by us; quantity checks depend on line items being extracted; AI calls run inline, not in a job queue; sandbox only, no real money; no mobile layout.

## Links
Repo: https://github.com/SabhyataC/paymaster | Demo: <hosted URL> | Judge login: <email / password> | Video: <link>
Test it: `python -m tests.test_hardening`, `python -m tests.test_bugfixes`, `python -m tests.test_trust`, `python -m tests.test_report`, `python -m scripts.eval --stub-ai` (on a database named *test* or *eval*).
