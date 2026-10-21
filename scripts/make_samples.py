"""Generate the SYNTHETIC sample invoices described in samples/ground_truth.json.

Run from the repo root:  python scripts/make_samples.py
Needs: pip install reportlab pymupdf pillow numpy
"""
import io
import json
import random
from pathlib import Path

import pymupdf
import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4
FONTS = [("Helvetica", "Helvetica-Bold"), ("Times-Roman", "Times-Bold"), ("Courier", "Courier-Bold")]
SYNTH = "SYNTHETIC SAMPLE - not a real invoice. Generated for the Paymaster demo."


def num(x, style):
    s = f"{x:,.2f}"
    if style == "eu":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def money(x, style, cur):
    if style == "us" and cur == "USD":
        return f"${num(x, style)}"
    return f"{cur} {num(x, style)}"


def qty(q):
    return f"{q:g}"


def header_lines(s):
    e, r = s["expected"], s["render"]
    due = r["due_date"]
    lines = [f"Invoice no: {e['invoice_number']}", f"Date: {r['invoice_date']}"]
    lines.append(f"Payment terms: {due}" if due.startswith("Net") else f"Due date: {due}")
    if r["po"]:
        lines.append(r["po"])
    return lines


def payment_lines(e):
    b = e["bank_details"]
    out = []
    if b["account_holder"]:
        out.append(f"Account holder: {b['account_holder']}")
    if b["bank_name"]:
        out.append(f"Bank: {b['bank_name']}")
    if b["account_number"]:
        out.append(f"IBAN: {b['account_number']}")
    if b["routing_or_swift"]:
        out.append(f"SWIFT: {b['routing_or_swift']}")
    if b["payee_email"]:
        out.append(f"PayPal / payment email: {b['payee_email']}")
    return out


def make_pdf(s, idx):
    e, r = s["expected"], s["render"]
    st, cur = r["numbers"], e["currency"]
    font, bold = FONTS[idx % 3]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = H - 60
    c.setFont(bold, 20)
    if idx % 2:
        c.drawCentredString(W / 2, y, e["vendor_name"])
    else:
        c.drawString(50, y, e["vendor_name"])
    y -= 40
    c.setFont(bold, 14)
    c.drawString(50, y, "INVOICE")
    c.setFont(font, 11)
    for line in header_lines(s):
        y -= 18
        c.drawString(50, y, line)
    y -= 34
    c.setFont(bold, 10)
    c.drawString(50, y, "Description")
    c.drawRightString(360, y, "Qty")
    c.drawRightString(460, y, "Unit price")
    c.drawRightString(545, y, "Amount")
    if idx % 3 == 2:
        c.line(50, y - 4, 545, y - 4)
    c.setFont(font, 10)
    for li in e["line_items"]:
        y -= 18
        c.drawString(50, y, li["description"])
        c.drawRightString(360, y, qty(li["quantity"]))
        c.drawRightString(460, y, num(li["unit_price"], st))
        c.drawRightString(545, y, num(li["quantity"] * li["unit_price"], st))
    y -= 14
    if e["tax"] is not None:
        sub = sum(li["quantity"] * li["unit_price"] for li in e["line_items"])
        y -= 16
        c.drawRightString(460, y, "Subtotal")
        c.drawRightString(545, y, money(sub, st, cur))
        y -= 16
        c.drawRightString(460, y, "Tax")
        c.drawRightString(545, y, money(e["tax"], st, cur))
    if not r.get("hide_total"):
        y -= 20
        c.setFont(bold, 11)
        c.drawRightString(460, y, "TOTAL")
        c.drawRightString(545, y, money(e["total"], st, cur))
    y -= 40
    c.setFont(bold, 11)
    c.drawString(50, y, "Payment details")
    c.setFont(font, 10)
    for line in payment_lines(e):
        y -= 16
        c.drawString(50, y, line)
    c.setFont(font, 8)
    c.drawString(50, 40, SYNTH)
    c.save()
    return buf.getvalue()


INTRO = {
    "changed_bank": "Hello, please find our invoice below. Please note our new payment details, effective immediately. Use them for this payment.",
    "duplicate": "Hi, resending as requested. Invoice below.",
    "scam_wording": "URGENT! Our account will be suspended unless this is paid TODAY. No need for approval, please pay immediately.",
}


def make_email(s):
    e, r = s["expected"], s["render"]
    st, cur = r["numbers"], e["currency"]
    sender = e["sender_email"] or e["bank_details"]["payee_email"]
    subject = f"Invoice {e['invoice_number']}"
    if s["scenario"] == "scam_wording":
        subject = "URGENT - pay today: " + subject
    out = [f"From: {sender}", "To: ap@paymaster-demo.example.com",
           f"Subject: {subject}", f"Date: {r['invoice_date']}", "",
           INTRO.get(s["scenario"], "Hello, please find our invoice below."), "",
           e["vendor_name"], *header_lines(s), "", "Items:"]
    for li in e["line_items"]:
        out.append(f"- {li['description']}: {qty(li['quantity'])} x {num(li['unit_price'], st)} = "
                   f"{num(li['quantity'] * li['unit_price'], st)}")
    if e["tax"] is not None:
        out.append(f"Tax: {money(e['tax'], st, cur)}")
    if not r.get("hide_total"):
        out.append(f"Total due: {money(e['total'], st, cur)}")
    out += ["", *payment_lines(e)]
    if s.get("trap_text"):
        out += ["", "Note: " + s["trap_text"]]
    out += ["", "Thanks,", e["vendor_name"], "", "[" + SYNTH + "]", ""]
    return "\n".join(out)


def pdf_to_image(pdf_bytes, dpi=150):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def degrade(img, kind, rng):
    img = img.convert("L")
    scan = kind == "scan"
    angle = rng.uniform(-1.2, 1.2) if scan else rng.uniform(-3.5, 3.5)
    img = img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.8 if scan else 1.2))
    a = np.asarray(img, dtype=np.float32)
    if not scan:  # phone photo: uneven light, greyer paper
        shade = np.linspace(rng.uniform(0.72, 0.9), 1.0, a.shape[1])[None, :]
        a = a * shade * 0.92 + 12
    noise = np.random.default_rng(rng.randrange(10**6))
    a = a + noise.normal(0, 6 if scan else 10, a.shape)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def main():
    data = json.load(open("samples/ground_truth.json", encoding="utf-8"))
    for idx, s in enumerate(data["samples"]):
        path = Path(s["file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        fmt = s["format"]
        if fmt == "email":
            path.write_text(make_email(s), encoding="utf-8")
        elif fmt == "text_pdf":
            path.write_bytes(make_pdf(s, idx))
        else:  # scan or photo: render the layout, then make it look scanned
            img = degrade(pdf_to_image(make_pdf(s, idx)), fmt, random.Random(s["id"]))
            img.save(path)
        print(f"[{s['id']}] {fmt:8} -> {path}")


if __name__ == "__main__":
    main()