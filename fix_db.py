from backend.db import get_conn

with get_conn() as conn:
    conn.execute(open("db/schema.sql", encoding="utf-8").read())
print("done")