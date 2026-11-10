"""Tamper-evident audit log: sealing, verification and the dev-only reset.
The rules live in db/schema.sql (append-only triggers, audit_seal(), audit_verify())."""
import os
import threading
import time
from contextlib import contextmanager

from backend.db import DATABASE_URL, get_conn


def seal(conn) -> int:
    """Chain every committed audit row that is not chained yet. Safe to call at any time, from any process."""
    n = conn.execute("SELECT audit_seal()").fetchone()[0]
    conn.commit()
    return n


def verify(conn, company_id) -> dict:
    seal(conn)
    r = conn.execute("SELECT * FROM audit_verify(%s)", (company_id,)).fetchone()
    ok, checked, broken, reason, head, unsealed = r
    first = conn.execute("SELECT min(created_at) FROM audit_log WHERE company_id = %s AND seal_seq IS NOT NULL", (company_id,)).fetchone()[0]
    return {"ok": ok, "checked": checked, "broken_at": broken, "reason": reason, "head": head if checked else None,
            "waiting_to_be_sealed": unsealed, "chain_started": first.isoformat() if first else None}


def start_sealer(every: float = 5.0):
    """Background loop: keeps the chain close to real time. AUDIT_SEAL_WORKER=0 turns it off."""
    if os.environ.get("AUDIT_SEAL_WORKER", "1") == "0":
        return

    def loop():
        while True:
            time.sleep(every)
            try:
                with get_conn() as conn:
                    seal(conn)
            except Exception:
                pass
    threading.Thread(target=loop, name="audit-sealer", daemon=True).start()


@contextmanager
def allow_reset(conn):
    """DEV / TEST ONLY: lets a script wipe the audit log (seed, eval). Needs table ownership, so the app's
    normal runtime role cannot do it. Refuses unless the database name says it is a throw-away one,
    or the caller passes force after its own safety check (the seed script does)."""
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
    conn.execute("ALTER TABLE ledger_entries DISABLE TRIGGER USER")
    try:
        yield
        conn.execute("DELETE FROM audit_heads")
        conn.execute("UPDATE audit_log SET seal_seq = NULL, prev_hash = NULL, row_hash = NULL")   # re-chain what is left
    finally:
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
        conn.execute("ALTER TABLE ledger_entries ENABLE TRIGGER USER")


def reset_guard():
    name = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if not any(w in name for w in ("test", "eval", "dev", "demo", "seed")) and os.environ.get("ALLOW_AUDIT_RESET") != "1":
        raise SystemExit(f"Refusing to wipe the audit log of database '{name}'. Set ALLOW_AUDIT_RESET=1 if you are sure.")
