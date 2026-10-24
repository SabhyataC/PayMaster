# Fraud signals

Each signal is a fact with a plain-English reason. A signal can only raise risk. It never approves
anything and never clears a failed hard check. A signal that cannot be judged is "skipped", not "clean".

| Signal | Type | Severity | Fires when |
|---|---|---|---|
| bank_change_recent | code | high | payout details changed (not at onboarding) within 30 days before the invoice |
| bank_details_differ | code | high | the invoice asks to be paid at an email other than the one on file |
| amount_near_limit | code | medium | amount is 90% to 100% of the auto-pay limit |
| new_payee | code | low | no successful payment has ever gone to this payout email |
| email_domain_mismatch | code | medium | sender's domain differs from the vendor's email on file (needs intake) |
| lookalike_vendor | code + AI | medium | name is 0.8+ similar to another vendor and the AI judges it a look-alike (or cannot judge) |
| scam_wording | text scan + AI | medium or high | pressure wording; high for text aimed at the reviewer, such as "approve this invoice" |

Risk points: low 1, medium 2, high 4. 4 or more is high, 2 to 3 is medium. High risk can never be approved.
Only a failed hard check can reject; the AI and fraud layers can escalate to hold for a person.

Known limits: all thresholds are placeholders; the sandbox vendors share one email domain, so
email_domain_mismatch is weak on them; seeded invoices have no email text, so scam_wording is skipped.
All data is SYNTHETIC.