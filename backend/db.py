import os
from contextlib import contextmanager

import psycopg
from dotenv import load_dotenv

load_dotenv()  # reads .env from the folder you run the app from (repo root)

DATABASE_URL = os.environ["DATABASE_URL"]


@contextmanager
def get_conn():
    """Open a connection, commit if all went well, roll back if anything failed."""
    with psycopg.connect(DATABASE_URL) as conn:
        yield conn