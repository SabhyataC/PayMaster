# User guide: add your own vendor, PO and invoice

This is the manual path, for when you want to try PayMaster with your own data instead of the **Add demo data** button. All data is synthetic and payments go to the PayPal sandbox.

**Who can do what**

| Role | Can do |
|---|---|
| Admin | Everything: vendors, purchase orders, deliveries, people, settings, upload, decide, pay |
| Reviewer | Upload invoices, approve or reject, release payments, resolve exceptions |
| Viewer | Read only |

An invoice is only accepted when its **vendor** and **PO number** already exist in the system, so set those up first.

## 1. Add a vendor (Admin)

1. Sidebar: **Vendors & POs**, tab **Vendors**.
2. Fill in **Name**, **Country** and **Payout (PayPal) email**, then press **Add vendor**.
   - The name must match the name printed on the invoice (case does not matter).
   - The payout email is where the money goes. For the sandbox use one of your PayPal sandbox Personal accounts, for example `paymaster-vendor-us@example.com`.
3. Changing a vendor's payout email later is a sensitive change. With two or more admins, a second admin must approve it under **Waiting for approval**.

## 2. Create a purchase order (Admin)

1. Tab **Purchase orders**.
2. Choose the vendor, enter a **PO number** (for example `PO-5001`), the **Amount**, the **currency** (USD, GBP or EUR) and, if you want the quantity check, a **Quantity**. Press **Create PO**.
3. Invoices are accepted when the amount is within 2% of the PO.

## 3. Record the delivery (Admin)

1. Tab **Deliveries**.
2. Choose the PO, write what arrived, the **Value** and optionally the **Quantity**, then press **Record delivery**. Or use **Mark fully delivered** on the PO row.
3. An invoice for goods that have not been delivered is held ("billed before delivery"). Skip this step on purpose to see that happen.

## 4. Upload an invoice (Reviewer or Admin)

1. Sidebar: **Invoices**. Choose a file and press **Upload invoice**.
2. Accepted files: PDF (up to 20 pages), PNG, JPG, or a plain-text email (`.txt` or `.eml`), up to 10 MB. The same file cannot be uploaded twice.
3. Wait up to a minute. Gemini reads the invoice, then Verify runs. The message under the button shows the result.

Want something to upload? Copy this into `invoice.txt`, after you created vendor **Acme Supplies** with payout email `paymaster-vendor-us@example.com`, PO `PO-5001` for `500.00` USD, and a delivery of `500.00`:

```text
From: paymaster-vendor-us@example.com
To: ap@example.com
Subject: Invoice AC-1001

Acme Supplies
Invoice no: AC-1001
Date: 12 November 2026
PO-5001

Items:
- Office chairs: 5 x 100.00 = 500.00
Total due: $500.00

Account holder: Acme Supplies
PayPal / payment email: paymaster-vendor-us@example.com

[SYNTHETIC SAMPLE]
```

Ready-made samples (PDF, scanned PNG and email) are in the repo's [`samples/`](../samples) folder. They belong to the seeded demo company, so their vendors and POs exist only after `python -m backend.seed` or the demo data button.

## 5. Read the result

| What you see | What it means | What to do |
|---|---|---|
| **Approved** | Every check passed | Press **Pay** in the review queue (or open the invoice) |
| **Held** | A check or fraud signal needs a person, or the AI was unavailable | Open **Review queue**, press **Trust graph** to see why, then **Approve** or **Reject** |
| **Rejected** | A hard check failed (no PO, wrong vendor, duplicate...) | Read the reason; fix the data and upload again |
| **Needs review** (upload list) | The AI could not read it confidently, or the vendor or PO is not in the system | Add the missing vendor or PO, or press **Retry** or delete the upload |

## 6. Decide and pay

1. **Review queue**: for a held invoice press **Approve** or **Reject** and write a reason of at least 10 characters. A named person is then on the record.
2. Press **Pay**. Amounts over the auto-pay limit (default 5000.00, change it on the **Team & settings** page) or payments to a new payee stop at a policy hold until a person releases them.
3. PayPal sends a webhook when the payout succeeds. The invoice becomes **Paid** and the ledger rows appear under **Payments & ledger**.
4. Check **Reconciliation** and **Audit integrity** to see the books match PayPal and the audit chain is intact.

## Troubleshooting

- **"vendor ... is not in the vendor list" / "PO ... is not in the system":** create it (steps 1 and 2) and press **Retry** on the upload, or upload again.
- **Held with an AI or connection error:** press **Re-verify** on the invoice.
- **Payment stays "pending":** the PayPal webhook is not configured, or `PAYPAL_WEBHOOK_ID` is wrong. See [operations.md](operations.md).
- **Buttons missing:** your role cannot do that action. Viewers are read only, and only admins manage vendors and POs.
