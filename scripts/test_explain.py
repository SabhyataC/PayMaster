from backend.db import get_conn
from backend.explain import explain_exception

with get_conn() as conn:
    row = conn.execute("SELECT id FROM exceptions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        print("No exceptions yet - we plant one in piece 4")
    else:
        print(explain_exception(conn, row[0]))
        conn.commit()