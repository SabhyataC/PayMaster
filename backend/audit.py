from psycopg.types.json import Jsonb


def log_decision(
    conn,
    *,
    stage: str,          # verify, pay, reconcile, advise
    entity_type: str,    # invoice, payment, vendor ...
    entity_id,           # the UUID of that invoice/payment/vendor
    decision: str,       # approved, flagged, held, paid ...
    reason: str,         # plain-English why
    inputs: dict | None = None,   # the data the decision was based on
    actor: str = "system",        # system, ai, human
):
    """Write one row to audit_log, inside the caller's transaction."""
    conn.execute(
        """
        INSERT INTO audit_log
            (stage, entity_type, entity_id, decision, reason, inputs, actor)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (stage, entity_type, entity_id, decision, reason, Jsonb(inputs or {}), actor),
    )