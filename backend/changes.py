from fastapi import HTTPException

from backend.audit import log_decision


def request_vendor_email(conn, user, vendor_id, new_email):
    """Ask for a payout email change. A different admin must approve it.
    Returns None (no change), "applied" (sole admin, applied now) or the request id."""
    new = new_email.strip().lower()
    cur = conn.execute(
        "SELECT destination_email FROM vendor_bank_history WHERE vendor_id::text = %s AND valid_to IS NULL",
        (vendor_id,)).fetchone()
    if cur and cur[0].lower() == new:
        return None
    waiting = conn.execute(
        "SELECT 1 FROM pending_changes WHERE kind = 'vendor_email' AND target_id::text = %s AND status = 'pending'",
        (vendor_id,)).fetchone()
    if waiting:
        raise HTTPException(409, "A payout email change for this vendor is already waiting for approval")

    admins = conn.execute(
        "SELECT count(*) FROM users WHERE company_id = %s AND role = 'admin' AND disabled_at IS NULL",
        (user["company_id"],)).fetchone()[0]
    if admins < 2:   # nobody else could approve: apply now, but leave a loud audit trail
        apply_vendor_email(conn, user["company_id"], vendor_id, new, user["full_name"],
                           "nobody (sole admin, no second approver)")
        log_decision(conn, stage="vendor", entity_type="vendor", entity_id=vendor_id,
                     decision="payout_email_changed_without_second_approval",
                     reason=f"WARNING: {user['full_name']} is the only admin, so this payout email change "
                            f"to {new} had no second approver",
                     inputs={"by": user["id"], "old": cur[0] if cur else None, "new": new},
                     actor="human")
        return "applied"

    row = conn.execute(
        """INSERT INTO pending_changes (company_id, kind, target_id, payload, requested_by)
           VALUES (%s, 'vendor_email', %s::uuid, jsonb_build_object('new_email', %s::text, 'old_email', %s::text), %s)
           RETURNING id""",
        (user["company_id"], vendor_id, new, cur[0] if cur else None, user["id"])).fetchone()
    log_decision(conn, stage="vendor", entity_type="vendor", entity_id=vendor_id,
                 decision="payout_email_requested",
                 reason=f"{user['full_name']} asked to change the payout email from "
                        f"{cur[0] if cur else 'none'} to {new}; waiting for a second admin",
                 inputs={"by": user["id"], "old": cur[0] if cur else None, "new": new},
                 actor="human")
    return row[0]

def apply_vendor_email(conn, company_id, vendor_id, new_email, requested_by, approved_by):
    """Do the actual change, using the same history rows as before."""
    old = conn.execute(
        "SELECT destination_email FROM vendor_bank_history WHERE vendor_id::text = %s AND valid_to IS NULL",
        (vendor_id,)).fetchone()
    conn.execute("UPDATE vendor_bank_history SET valid_to = now() "
                 "WHERE vendor_id::text = %s AND valid_to IS NULL", (vendor_id,))
    conn.execute("INSERT INTO vendor_bank_history (vendor_id, destination_email, change_source, company_id) "
                 "VALUES (%s, %s, 'manual_edit', %s)", (vendor_id, new_email, company_id))
    log_decision(conn, stage="vendor", entity_type="vendor", entity_id=vendor_id,
                 decision="payout_email_changed",
                 reason=f"{requested_by} asked and {approved_by} approved: payout email changed from "
                        f"{old[0] if old else 'none'} to {new_email}",
                 inputs={"old": old[0] if old else None, "new": new_email},
                 actor="human")