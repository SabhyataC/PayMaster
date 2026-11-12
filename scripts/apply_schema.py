"""Applies db/schema.sql to DATABASE_URL. Safe to run on every start (every statement is idempotent).
    python -m scripts.apply_schema"""
import os
import sys
import time
from pathlib import Path

import psycopg

SQL = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8")

for attempt in range(1, 11):     # the database may still be starting
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
            c.execute(SQL)
        print("schema applied")
        break
    except psycopg.OperationalError as e:
        print(f"database not ready ({attempt}/10): {e}")
        time.sleep(3)
else:
    sys.exit("could not reach the database")
