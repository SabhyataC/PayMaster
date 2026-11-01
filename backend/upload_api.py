"""Upload an invoice (PDF, scan, or email text): Intake reads it, Verify checks it."""
import hashlib
from pathlib import Path
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from backend.pay import pay_invoice
from backend.auth import current_member
from backend.db import get_conn
from backend.intake import extract_row

router = APIRouter(prefix="/invoices", tags=["upload"])

MAX_BYTES = 10 * 1024 * 1024
FORMATS = {".pdf": "text_pdf", ".png": "scan", ".jpg": "scan", ".jpeg": "scan",
           ".txt": "email", ".eml": "email"}


@router.post("/upload")
def upload_invoice(file: UploadFile = File(...), user: dict = Depends(current_member)):
    name = Path(file.filename or "").name
    fmt = FORMATS.get(Path(name).suffix.lower())
    if not fmt:
        raise HTTPException(422, "Upload a PDF, PNG, JPG, or an email as .txt/.eml")
    raw = file.file.read(MAX_BYTES + 1)
    if not raw:
        raise HTTPException(422, "The file is empty")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")

    sha = hashlib.sha256(raw).hexdigest()
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM extractions WHERE source_sha256 = %s AND company_id = %s "
                       "AND status NOT IN ('failed', 'needs_review')",
                        (sha, user["company_id"])).fetchone():
            raise HTTPException(409, "This exact file was already uploaded")
        conn.execute("DELETE FROM extractions WHERE source_sha256 = %s AND company_id = %s "
                     "AND status IN ('failed', 'needs_review')", (sha, user["company_id"]))
        text = raw.decode("utf-8", errors="replace") if fmt == "email" else None
        blob = None if fmt == "email" else raw
        ext_id = conn.execute(
            "INSERT INTO extractions (source_name, source_format, source_text, source_bytes, "
            "source_sha256, status, is_synthetic, company_id) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s) RETURNING id",
            (name, fmt, text, blob, sha, user["company_id"]),
        ).fetchone()[0]

    res = extract_row(ext_id)                      # Intake: AI read + route (creates the invoice)
    routing = res["routing"]
    out = {"extraction_id": str(ext_id), "file": name, "intake": routing["status"],
           "reason": routing.get("reason"), "invoice_id": None,
           "invoice_status": None, "verdict": None, "verdict_reasons": []}

    if routing["status"] == "accepted" and routing.get("invoice_id"):
        from backend.investigator import investigate  # imported here: it pulls in the AI client
        v = investigate(routing["invoice_id"])         # Verify
        out.update(invoice_id=str(routing["invoice_id"]), invoice_status=v["status"],
                   verdict=v["verdict"]["decision"], verdict_reasons=v["verdict"]["reasons"])
        out["payment"] = None
        if v["status"] == "approved":                  # Pay: no human named, so policy holds/blocks as usual
            try:
                with get_conn() as conn:
                    p = pay_invoice(conn, routing["invoice_id"])
                out["payment"] = p.get("status")
                out["payment_reasons"] = p.get("reasons", [])
            except Exception as e:
                out["payment"] = "error"
                out["payment_reasons"] = [str(e)[:150]]
    return out