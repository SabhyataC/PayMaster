## Findings

1. FAILED: India business seller sending a payout got 403 PAYOUT_NOT_AVAILABLE ("country not allowed to send this payout"). The cause is the sender account's country.

2. WORKED: US business seller sending a payout got 201, and the first payout completed successfully.

3. Payouts work from a US business seller to recipients in the US, India and Germany. Four items reached SUCCESS, including paymaster-buyer-in and paymaster-buyer-gm. All were sent in USD. Currency conversion was tested later (see finding 14).

4. An unregistered recipient gives UNCLAIMED, not a failure. The error is RECEIVER_UNREGISTERED. PayPal sends the recipient a sign-up link, and the message says the funds return to you if they aren’t claimed within 30 days.

5. The batch status says SUCCESS even though one item is UNCLAIMED. This matters for the design: Paymaster must read the status of each item, not the batch’s. If it trusts the batch status, it would mark an unpaid invoice as paid.

6. Running the same script twice paid twice, because each run made a new batch ID and a new request ID. Paymaster must reuse the same IDs when it retries.

7. A payout is not instant. It goes PENDING, then PROCESSING, then SUCCESS, and it took a few minutes.

8. Simulated failure (note = ERRPYO005): PayPal refused the request immediately with HTTP 422, name INSUFFICIENT_FUNDS. No batch was created. The "message" text did not match the name (it said "internal service error"), so Paymaster should rely on the name, not the message. Meaning: invoice stays unpaid and needs a person. Retrying won't help.

- Paymaster should mark the invoice as “not paid, needs attention” and stop when this happens. It shouldn’t retry, because retrying won’t add money to the balance. A person has to top up the account first.

9. Simulated timeout (note = ERRPYO036): PayPal's front door returned HTTP 504 as an HTML web page, not data. This means "outcome unknown": the payment may or may not have happened. Paymaster must (a) not assume replies are readable data, and (b) retry with the SAME request ID and batch ID, never fresh ones, to avoid paying twice.
===== LESSON =====
- The reply isn’t always in PayPal’s format. My scripts assume the reply is data they can read, and the failed-payout script only worked because it printed the raw text. If send_payout.py had hit this, it would have crashed trying to read a web page as data. The real service must check what kind of reply it got before reading it.
- After a timeout, don’t just send it again. A new attempt with fresh IDs would be treated as a second payment. If the first one did go through, the vendor gets paid twice. The safe way is to send the retry with the same IDs as the first attempt, so PayPal recognises it as the same payment. We’ll build that and test it on Wednesday. I haven’t verified exactly how PayPal responds to a repeated ID, so I won’t promise how it behaves until we test it.


10. Webhooks worked (Oct 19). Tunnel: cloudflared -> uvicorn receiver on port 8000, registered in PayPal on app paymaster-us (route /webhooks/paypal, All Events). One test payout produced two events, both answered 200 OK:
    - PAYMENT.PAYOUTS-ITEM.SUCCEEDED (the single payout reached the recipient)
    - PAYMENT.PAYOUTSBATCH.SUCCESS (the whole batch finished), about 7 seconds after the item event in this one test
    The polling script had stopped while the batch was still PROCESSING, but the webhook still delivered the final SUCCESS. So Paymaster's Reconcile stage should rely on webhooks, with polling only as a backup.
    The calls came from PayPal addresses starting with 173.0.80.
===== LESSON =====
- Webhooks are the source of truth for the final result, not the script's polling. Item events matter more than batch events (same reason as finding 5).

11. Transaction Search works (Oct 19). GET /v1/reporting/transactions returned 200 with the Oct 18 payouts. Each payout appears as three rows: T1503 (temporary hold of amount + fees), T0001 (the payment), T1105 (hold released). Reconcile should use only T0001 rows. A T0001 row with status P (pending) appeared, probably the UNCLAIMED item (to confirm by matching its transaction ID). Search results lag behind real time: the Oct 19 payout had not appeared yet.

12. Cancel works for UNCLAIMED items (Oct 19). PayPal's Refund API only covers money received, not payouts sent, so the test used POST /v1/payments/payouts-item/{id}/cancel. Cancelling while the batch was still PROCESSING failed: 400 BATCH_NOT_COMPLETED ("Only item belonging to a batch in Processed status can be cancelled"), even though the item already showed UNCLAIMED. After the batch reached SUCCESS, cancel returned 200 and the item became RETURNED. The old RECEIVER_UNREGISTERED error text stays in the reply, so use transaction_status, not the errors block. Only UNCLAIMED items can be cancelled; a payout that reached a registered recipient cannot be reversed this way (per PayPal docs, not tested). Leftover: item DRKKKNQA2Y4G6 ($5) stays UNCLAIMED and returns on its own after 30 days.
===== LESSON =====
- Cancel only after the BATCH is complete, not just when the item looks UNCLAIMED.

13. Invoicing works (Oct 19). POST /v2/invoicing/invoices created a draft (201), POST .../send returned 200, and GET showed status SENT; the payer-view link opened in a browser. This is the receivable side (invoices you send to get paid), so it is not part of Paymaster's main flow. Decision: park it, and use made-up vendor invoice files as Verify's input.

14. Multi-currency works (Oct 19). A 10.00 EUR payout from the USD business account to paymaster-buyer-gm reached SUCCESS. The item details include currency_conversion: from_amount 13.08 USD, to_amount 10.20 EUR, exchange_rate 0.78, and payout_item_fee 0.20 EUR (the 10.20 EUR matches payout + fee). So the sender is debited 13.08 USD for a 10.00 EUR invoice, and the USD cost is only known after sending. Paymaster must record invoice amount + currency, USD debited, rate and fee for each payout. Sandbox rates may differ from real ones. Not checked yet: the recipient's balance, and how Transaction Search shows a converted payout.

## Note: 
PayPal calls a Personal sandbox account a “buyer” and a Business account a “merchant/seller”. That only describes the account type. Payouts must be sent from a Business account, and recipients can be Personal accounts. So the Business account is the sender, and the Personal accounts receive.

## Not tested yet
- Whether PayPal sends the same webhook twice, or sends events out of order (the receiver should handle both before the demo)
- The receiver does not verify that a message really came from PayPal (signature verification not built yet)
- The tunnel URL changes every time cloudflared restarts, so the webhook URL in PayPal must be updated each time
- Events for UNCLAIMED and failed items (only the success path was tested)
- How Transaction Search shows a converted payout (re-run search_transactions.py in a few hours; it should also show today's payouts)

## Decisions
- The synthetic company will be based in a country that can send payouts (US), not India.
- Webhook testing via a free Cloudflare tunnel, not Render, for now (tested Oct 19, works).
- Invoicing: not used for the main flow (works, but wrong direction for accounts payable).


## Steps to start the Cloudflared and Uvicorn

1. Terminal 1: receiver
If the old uvicorn is still running, press Ctrl+C in it first. Then run:

.venv\Scripts\activate
uvicorn webhook_receiver:app --port 8000

2. Terminal 2: tunnel
If the old cloudflared is still running, press Ctrl+C in it first. Then run:

cloudflared tunnel --url http://localhost:8000

- Important: cloudflared prints a new https://....trycloudflare.com URL every time you restart it, so the old one registered in PayPal is now dead. You need to update it there:

i. Go to developer.paypal.com, then Apps & Credentials (Sandbox), then app paymaster-us.
ii. Scroll to Webhooks and click Edit on the existing webhook (don’t add a new one).
iii. Replace the URL with https://<new-url>/webhooks/paypal, with no spaces, and save.