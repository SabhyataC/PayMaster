"""Upload an invoice (PDF, scan, or email text): Intake reads it, Verify checks it."""
import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.auth import current_member, reviewer_or_admin
from backend.db import get_conn
from backend.intake import extract_row
from backend.pay import pay_invoice
from backend.upload_checks import MAX_BYTES, UploadRejected, validate_upload

router = APIRouter(prefix="/invoices", tags=["upload"])

MAX_UPLOADS_PER_USER_HOUR = 30
MAX_UPLOADS_PER_COMPANY_DAY = 300


@router.post("/upload")
def upload_invoice(file: UploadFile = File(...), user: dict = Depends(reviewer_or_admin)):
    raw = file.file.read(MAX_BYTES + 1)
    try:
        name, fmt = validate_upload(file.filename or "", raw)
    except UploadRejected as e:
        raise HTTPException(e.status, e.message)

    sha = hashlib.sha256(raw).hexdigest()
    with get_conn() as conn:
        per_user = conn.execute(
            "SELECT count(*) FROM extractions WHERE uploaded_by = %s AND created_at > now() - interval '1 hour'",
            (user["id"],)).fetchone()[0]
        per_company = conn.execute(
            "SELECT count(*) FROM extractions WHERE company_id = %s AND created_at > now() - interval '1 day'",
            (user["company_id"],)).fetchone()[0]
        if per_user >= MAX_UPLOADS_PER_USER_HOUR or per_company >= MAX_UPLOADS_PER_COMPANY_DAY:
            raise HTTPException(429, "Upload limit reached. Try again later")
        if conn.execute("SELECT 1 FROM extractions WHERE source_sha256 = %s AND company_id = %s "
                        "AND status NOT IN ('failed', 'needs_review')",
                        (sha, user["company_id"])).fetchone():
            raise HTTPException(409, "This exact file was already uploaded")
        conn.execute("DELETE FROM extractions WHERE source_sha256 = %s AND company_id = %s "
                     "AND status IN ('failed', 'needs_review')", (sha, user["company_id"]))
        text = raw.decode("utf-8") if fmt == "email" else None
        blob = None if fmt == "email" else raw
        ext_id = conn.execute(
            "INSERT INTO extractions (source_name, source_format, source_text, source_bytes, "
            "source_sha256, status, is_synthetic, company_id, uploaded_by) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s, %s) RETURNING id",
            (name, fmt, text, blob, sha, user["company_id"], user["id"]),
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
