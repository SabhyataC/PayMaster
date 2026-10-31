"""Needs-review flow: a person fixes extracted fields. The first AI values are kept, the code
checks still run on the fixed values, and Verify runs again on any invoice that results."""
import json

from backend.audit import log_decision
from backend.db import get_conn
from backend.intake import FIELDS, route_row, score_extraction

MIN_NOTE = 10


class CorrectionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def correct_extraction(extraction_id, reviewer, note, corrections, company_id):
    reviewer, note = (reviewer or "").strip(), (note or "").strip()
    if not reviewer:
        raise CorrectionError("reviewer_required", "Say who is correcting")
    if len(note) < MIN_NOTE:
        raise CorrectionError("note_too_short", f"Write at least {MIN_NOTE} characters explaining why")
    if not corrections:
        raise CorrectionError("nothing_to_correct", "Send at least one field to correct")
    unknown = [k for k in corrections if k not in FIELDS]
    if unknown:
        raise CorrectionError("unknown_field", "Unknown field(s): " + ", ".join(unknown))

    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, extracted, original_extracted, field_confidence "
            "FROM extractions WHERE id = %s AND company_id = %s", (extraction_id, company_id)).fetchone()
        if row is None:
            raise CorrectionError("not_found", "No such extraction")
        status, fields, original, field_conf = row
        if status != "needs_review":
            raise CorrectionError("not_allowed", f"Only needs_review extractions can be corrected (this one is '{status}')")

        fields = dict(fields or {})
        original = original or dict(fields)          # keep the FIRST AI values only
        fields.update(corrections)
        conf = dict(field_conf or {})
        for k in corrections:
            conf[k] = 1.0                            # a person checked these fields...
        overall, per_field, _ = score_extraction(fields, conf)   # ...but code checks still apply

        conn.execute(
            """UPDATE extractions SET extracted = %s::jsonb, original_extracted = %s::jsonb,
                      field_confidence = %s::jsonb, confidence = %s, status = 'extracted', reason = NULL
               WHERE id = %s""",
            (json.dumps(fields), json.dumps(original), json.dumps(per_field), overall, extraction_id))
        diff = {k: {"original": original.get(k), "corrected": v} for k, v in corrections.items()}
        log_decision(
            conn, stage="intake", entity_type="extraction", entity_id=extraction_id,
            decision="corrected", actor="human",
            reason=f"{reviewer} corrected {', '.join(sorted(corrections))}: {note}",
            inputs={"reviewer": reviewer, "note": note, "corrections": diff})
        conn.commit()

    result = route_row(extraction_id)                # may land in needs_review again
    out = {"corrected": sorted(corrections), "status": result["status"],
           "reason": result.get("reason"), "invoice_id": None, "verify": None}
    if result.get("invoice_id"):
        from backend.investigator import investigate   # imported here: it pulls in the AI client
        out["invoice_id"] = str(result["invoice_id"])
        out["verify"] = investigate(result["invoice_id"])
    return out