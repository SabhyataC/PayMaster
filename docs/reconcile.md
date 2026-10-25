# Reconcile rules (SYNTHETIC sandbox data)

1. When a payout reaches SUCCESS, we debit accounts_payable by the invoice amount (we no longer owe the vendor) and credit paypal_balance by what PayPal took from our account.
2. PayPal's fee is a cost: debit fees_expense and credit paypal_balance, in the currency PayPal states the fee in (example: 0.20 EUR).
3. If our account currency differs from the vendor's, we bridge with an fx_clearing account and use only PayPal's numbers, never our own. Example from our sandbox test: 13.08 USD became 10.20 EUR at rate 0.78 (a 10.00 EUR payout plus a 0.20 EUR fee). USD rows: debit fx_clearing 13.08, credit paypal_balance 13.08. EUR rows: debit accounts_payable 10.00, debit fees_expense 0.20, credit fx_clearing 10.20. The conversion difference is not a profit or loss number; it is PayPal's stated rate, stored in the settlements row.
4. Every ledger row is in the currency of the money it describes, and for each payment, debits equal credits inside each currency.
5. If a payout is RETURNED, we add mirror rows (debit and credit swapped) for what PayPal says came back, the invoice goes back to approved, and the new attempt gets its own idempotency key.
6. Ledger rows are never edited or deleted; a mistake is fixed only by adding new reversing rows.