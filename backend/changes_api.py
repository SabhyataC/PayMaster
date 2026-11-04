from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from backend.audit import log_decision
from backend.auth import admin_only
from backend.changes import apply_vendor_email
from backend.db import get_conn
from backend.reprocess import reprocess_vendor

router = APIRouter(prefix="/changes", tags=["changes"])

PENDING = """SELECT c.id, c.target_id, c.payload, c.requested_by, u.full_name
             FROM pending_changes c JOIN users u ON u.id = c.requested_by
             WHERE c.id::text = %s AND c.company_id = %s AND c.status = 'pending' FOR UPDATE OF c"""


@router.get("")
def list_changes(user: dict = Depends(admin_only)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.target_id, v.name, c.payload, u.full_name, c.requested_by, c.created_at
               FROM pending_changes c JOIN vendors v ON v.id = c.target_id
               JOIN users u ON u.id = c.requested_by
               WHERE c.company_id = %s AND c.status = 'pending' ORDER BY c.created_at""",
            (user["company_id"],)).fetchall()
    return [{"id": str(r[0]), "vendor_id": str(r[1]), "vendor": r[2],
             "old_email": r[3].get("old_email"), "new_email": r[3].get("new_email"),
             "requested_by": r[4], "mine": str(r[5]) == str(user["id"]),
             "created_at": r[6].isoformat()} for r in rows]


@router.post("/{change_id}/approve")
def approve(change_id: str, background: BackgroundTasks, user: dict = Depends(admin_only)):
    with get_conn() as conn:
        row = conn.execute(PENDING, (change_id, user["company_id"])).fetchone()
        if not row:
            raise HTTPException(404, "No such pending change")
        cid, vendor_id, payload, requested_by, requester_name = row
        if str(requested_by) == str(user["id"]):
            raise HTTPException(403, "You asked for this change, so a different admin must approve it")
        apply_vendor_email(conn, user["company_id"], str(vendor_id), payload["new_email"],
                           requester_name, user["full_name"])
        conn.execute("UPDATE pending_changes SET status = 'approved', decided_by = %s, decided_at = now() "
                     "WHERE id = %s", (user["id"], cid))
    background.add_task(reprocess_vendor, user["company_id"], str(vendor_id))
    return {"status": "approved"}


@router.post("/{change_id}/reject")
def reject(change_id: str, user: dict = Depends(admin_only)):
    """Any admin can reject. The person who asked can use this to cancel their own request."""
    with get_conn() as conn:
        row = conn.execute(PENDING, (change_id, user["company_id"])).fetchone()
        if not row:
            raise HTTPException(404, "No such pending change")
        cid, vendor_id, payload, requested_by, requester_name = row
        conn.execute("UPDATE pending_changes SET status = 'rejected', decided_by = %s, decided_at = now() "
                     "WHERE id = %s", (user["id"], cid))
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=str(vendor_id),
                     decision="payout_email_rejected",
                     reason=f"{user['full_name']} rejected {requester_name}'s request to change the payout "
                            f"email to {payload['new_email']}",
                     inputs={"new": payload["new_email"]}, actor="human")
    return {"status": "rejected"}