# Reconcile rules (SYNTHETIC sandbox data)

1. When a payout reaches SUCCESS, we debit accounts_payable by the invoice amount (we no longer owe the vendor) and credit paypal_balance by what PayPal took from our account.
2. PayPal's fee is a cost: debit fees_expense and credit paypal_balance, in the currency PayPal states the fee in (example: 0.20 EUR).
3. If our account currency differs from the vendor's, we bridge with an fx_clearing account and use only PayPal's numbers, never our own. Example from our sandbox test: 13.08 USD became 10.20 EUR at rate 0.78 (a 10.00 EUR payout plus a 0.20 EUR fee). USD rows: debit fx_clearing 13.08, credit paypal_balance 13.08. EUR rows: debit accounts_payable 10.00, debit fees_expense 0.20, credit fx_clearing 10.20. The conversion difference is not a profit or loss number; it is PayPal's stated rate, stored in the settlements row.
4. Every ledger row is in the currency of the money it describes, and for each payment, debits equal credits inside each currency.
5. If a payout is RETURNED, we add mirror rows (debit and credit swapped) for what PayPal says came back, the invoice goes back to approved, and the new attempt gets its own idempotency key.
6. Ledger rows are never edited or deleted; a mistake is fixed only by adding new reversing rows.

## Day 16: exceptions and reconcile

`backend/reconcile.py` `run_reconcile(conn)` logs these exception types, one open row per payment per type:
`unknown_outcome` (30 min), `pending_too_long` (1 h), `unclaimed_too_long` (7 days),
`success_mismatch`, `amount_mismatch`, `unknown_payout` (PayPal payout we have no payment for).

- Payouts younger than 3 hours are ignored by the PayPal comparisons (Transaction Search lags).
- Unclaimed after 7 days: reconcile cancels the item, and once PayPal shows RETURNED the payment
  becomes `returned` and the exception is resolved. An unclaimed payment has no ledger rows,
  so nothing is reversed.
- A returned payment may be paid again: `payments.attempt` goes up by one and the key gets `-2`, `-3`, ...
  `failed` payments are never re-paid automatically.

Known gap: if a payment we booked as `success` is later RETURNED by PayPal, reconcile reports
`success_mismatch` but does not write reversing ledger rows yet.

## Day 17: exception API and AI explainer

Endpoints (all need the `X-Review-Token` header):
- `GET /exceptions?status=open|resolved`: list exceptions
- `GET /exceptions/{id}`: one exception, with its saved explanation
- `POST /exceptions/{id}/explain`: AI writes a plain-English explanation and a safe next step
- `POST /exceptions/{id}/resolve`: a person (`resolved_by`, `note`) closes it

The explainer is read-only: it never changes a payment or the ledger. If Gemini fails,
a fixed template text is saved instead. Every explain and resolve writes an audit row.

## Day 18: console

`console/index.html` is a single-page AG Grid console (no build step).
Run: start the API (`uvicorn backend.webhooks:app --port 8000`), then
`python -m http.server 5500 --directory console` and open http://localhost:5500.
Paste the review token (kept only in the browser tab). The exceptions grid has Explain
and Resolve; the review queue grid has Approve, Reject and Pay. CORS allows only localhost:5500.