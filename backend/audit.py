from psycopg.types.json import Jsonb


# which table holds the company for each kind of audited entity
_OWNER_SQL = {
    "invoice":    "SELECT company_id FROM invoices WHERE id = %s",
    "payment":    "SELECT company_id FROM payments WHERE id = %s",
    "extraction": "SELECT company_id FROM extractions WHERE id = %s",
    "vendor":     "SELECT company_id FROM vendors WHERE id = %s",
}


def log_decision(
    conn,
    *,
    stage: str,
    entity_type: str,
    entity_id,
    decision: str,
    reason: str,
    inputs: dict | None = None,
    actor: str = "system",
    company_id=None,   # pass it for entities with no owner row (reconcile_run, orphan exceptions)
):
    """Write one row to audit_log, inside the caller's transaction."""
    if company_id is None and entity_type in _OWNER_SQL:
        row = conn.execute(_OWNER_SQL[entity_type], (entity_id,)).fetchone()
        company_id = row[0] if row else None
    conn.execute(
        """
        INSERT INTO audit_log
            (stage, entity_type, entity_id, decision, reason, inputs, actor, company_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (stage, entity_type, entity_id, decision, reason, Jsonb(inputs or {}), actor, company_id),
    )