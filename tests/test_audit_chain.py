"""Append-only audit log and hash chain. Run on a TEST database:  python -m tests.test_audit_chain"""
import sys
import threading
from contextlib import contextmanager

import psycopg

from tests.test_hardening import RESULTS, bearer, check, client, one, run, section, world, raises  # noqa: F401
from backend import audit_chain
from backend.audit import log_decision
from backend.db import DATABASE_URL, get_conn


def put(cid, n=1, conn=None, tag="t"):
    def go(c):
        for i in range(n):
            log_decision(c, stage="admin", entity_type="company", entity_id=cid, decision="note", reason=f"{tag} {i}", inputs={"i": i})
    if conn is not None:
        return go(conn)
    with get_conn() as c:
        go(c); c.commit()


def verify(cid):
    with get_conn() as c:
        return audit_chain.verify(c, cid)


@contextmanager
def owner_bypass():
    """What someone with table ownership could do. The chain must still catch it."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        try:
            yield c
        finally:
            c.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")


def test_append_only():
    section("Audit rows cannot be changed, deleted or truncated")
    w = world(); put(w["cid"], 3)
    rid = one("SELECT min(id) FROM audit_log WHERE company_id = %s", (w["cid"],))[0]
    check("UPDATE of a row is refused", raises(lambda: run("UPDATE audit_log SET reason = 'edited' WHERE id = %s", (rid,)), psycopg.errors.InsufficientPrivilege) is True)
    check("UPDATE of the decision is refused", raises(lambda: run("UPDATE audit_log SET decision = 'approve' WHERE id = %s", (rid,)), psycopg.errors.InsufficientPrivilege) is True)
    check("DELETE of a row is refused", raises(lambda: run("DELETE FROM audit_log WHERE id = %s", (rid,)), psycopg.errors.InsufficientPrivilege) is True)
    check("DELETE of everything for a company is refused", raises(lambda: run("DELETE FROM audit_log WHERE company_id = %s", (w["cid"],)), psycopg.errors.InsufficientPrivilege) is True)
    check("TRUNCATE is refused", raises(lambda: run("TRUNCATE audit_log"), psycopg.errors.InsufficientPrivilege) is True)
    check("the row is untouched", one("SELECT reason FROM audit_log WHERE id = %s", (rid,))[0].startswith("t "))
    with get_conn() as c:
        audit_chain.seal(c)
    check("sealing a row is the one allowed change", one("SELECT seal_seq FROM audit_log WHERE id = %s", (rid,))[0] is not None)
    check("a sealed row cannot get a different hash", raises(lambda: run("UPDATE audit_log SET row_hash = 'x' WHERE id = %s", (rid,)), psycopg.errors.InsufficientPrivilege) is True)
    check("nor be un-sealed", raises(lambda: run("UPDATE audit_log SET seal_seq = NULL WHERE id = %s", (rid,)), psycopg.errors.InsufficientPrivilege) is True)


def test_chain_ok_and_isolated():
    section("The chain links rows in order and each company has its own")
    a, b = world(), world()
    put(a["cid"], 5); put(b["cid"], 2)
    va, vb = verify(a["cid"]), verify(b["cid"])
    check("company A: 5 rows chained, verified", va["ok"] and va["checked"] == 5 and va["waiting_to_be_sealed"] == 0, va)
    check("company B: its own chain of 2", vb["ok"] and vb["checked"] == 2 and vb["head"] != va["head"], vb)
    put(a["cid"], 3, tag="later")
    v2 = verify(a["cid"])
    check("new rows extend the same chain", v2["ok"] and v2["checked"] == 8, v2)
    rows = one("SELECT count(*) FROM audit_log WHERE company_id = %s AND prev_hash = (SELECT row_hash FROM audit_log x WHERE x.company_id = %s AND x.seal_seq = audit_log.seal_seq - 1)", (a["cid"], a["cid"]))[0]
    check("each row stores the hash of the one before", rows == 7, rows)
    empty = world()
    check("a company with no rows verifies as empty, not broken", verify(empty["cid"])["ok"] and verify(empty["cid"])["checked"] == 0)


def test_tampering_detected():
    section("Tampering by someone with full database access is detected")
    w = world(); put(w["cid"], 6); check("clean chain verifies", verify(w["cid"])["ok"])
    rid = one("SELECT id FROM audit_log WHERE company_id = %s AND seal_seq = 3", (w["cid"],))[0]
    with owner_bypass() as c:
        c.execute("UPDATE audit_log SET reason = 'nothing to see' WHERE id = %s", (rid,))
    v = verify(w["cid"])
    check("an edited row is caught, at the right position", v["ok"] is False and v["broken_at"] == 3 and "changed" in v["reason"], v)

    w = world(); put(w["cid"], 6); verify(w["cid"])
    with owner_bypass() as c:
        c.execute("UPDATE audit_log SET inputs = '{\"i\": 99}'::jsonb WHERE company_id = %s AND seal_seq = 5", (w["cid"],))
    check("an edited inputs field is caught too", verify(w["cid"])["broken_at"] == 5)

    w = world(); put(w["cid"], 6); verify(w["cid"])
    with owner_bypass() as c:
        c.execute("DELETE FROM audit_log WHERE company_id = %s AND seal_seq = 2", (w["cid"],))
    v = verify(w["cid"])
    check("a deleted row in the middle is caught", v["ok"] is False and v["broken_at"] == 2 and "missing" in v["reason"], v)

    w = world(); put(w["cid"], 6); verify(w["cid"])
    with owner_bypass() as c:
        c.execute("DELETE FROM audit_log WHERE company_id = %s AND seal_seq >= 5", (w["cid"],))
    v = verify(w["cid"])
    check("deleting the newest rows is caught (head record no longer matches)", v["ok"] is False and "removed" in v["reason"], v)

    w = world(); put(w["cid"], 4); verify(w["cid"])
    with owner_bypass() as c:
        for sql in ("UPDATE audit_log SET seal_seq = -1 WHERE company_id = %s AND seal_seq = 2",
                    "UPDATE audit_log SET seal_seq = 2 WHERE company_id = %s AND seal_seq = 3",
                    "UPDATE audit_log SET seal_seq = 3 WHERE company_id = %s AND seal_seq = -1"):
            c.execute(sql, (w["cid"],))
    check("swapping the order of two rows is caught", verify(w["cid"])["ok"] is False)

    w = world(); put(w["cid"], 4); verify(w["cid"])
    with owner_bypass() as c:
        c.execute("INSERT INTO audit_log (stage, entity_type, entity_id, decision, reason, company_id, seal_seq, prev_hash, row_hash) "
                  "VALUES ('admin','company', %s, 'note', 'forged', %s, 5, 'x', 'y')", (w["cid"], w["cid"]))
    check("a forged extra row is caught", verify(w["cid"])["ok"] is False)


def test_late_commit_never_skipped():
    section("A transaction that commits late is not skipped by the chain")
    w = world()
    slow = psycopg.connect(DATABASE_URL)            # open transaction, row not committed yet
    put(w["cid"], 1, conn=slow, tag="slow")
    put(w["cid"], 2, tag="fast")                     # committed after the slow row was written
    with get_conn() as c:
        sealed = audit_chain.seal(c)
    check("rows newer than an open transaction wait", sealed == 0 or one("SELECT count(*) FROM audit_log WHERE company_id = %s AND seal_seq IS NULL", (w["cid"],))[0] >= 2, sealed)
    slow.commit(); slow.close()
    v = verify(w["cid"])
    check("after it commits all three are chained and valid", v["ok"] and v["checked"] == 3 and v["waiting_to_be_sealed"] == 0, v)


def test_concurrent_writers():
    section("Many writers at once: no blocking between them, no gaps, chain valid")
    w = world(); errors = []

    def writer(k):
        try:
            for _ in range(10):
                put(w["cid"], 2, tag=f"w{k}")
        except Exception as e:
            errors.append(repr(e))
    ts = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
    [t.start() for t in ts]; [t.join(timeout=60) for t in ts]
    check("no writer failed or deadlocked", not errors and not any(t.is_alive() for t in ts), errors[:2])
    v = verify(w["cid"])
    check("all 160 rows are in one valid chain", v["ok"] and v["checked"] == 160, v)
    stop = []

    def sealer():
        for _ in range(15):
            with get_conn() as c:
                audit_chain.seal(c)
    w2 = world()
    t = threading.Thread(target=sealer); t.start()
    ts = [threading.Thread(target=lambda: put(w2["cid"], 15)) for _ in range(6)]
    [x.start() for x in ts]; [x.join(timeout=60) for x in ts]; t.join(timeout=60)
    v = verify(w2["cid"])
    check("sealing while writers are running still yields a valid chain of 90", v["ok"] and v["checked"] == 90, v)


def test_api():
    section("Verify endpoint")
    w = world(); put(w["cid"], 4)
    h = bearer(w["users"]["viewer"][0])
    r = client.get("/audit/chain/verify", headers=h)
    check("any member of the company can verify", r.status_code == 200 and r.json()["ok"] and r.json()["checked"] >= 4, r.text[:200])
    check("login is required", client.get("/audit/chain/verify").status_code in (401, 403))
    with owner_bypass() as c:
        c.execute("UPDATE audit_log SET reason = 'x' WHERE company_id = %s AND seal_seq = 2", (w["cid"],))
    r = client.get("/audit/chain/verify", headers=h).json()
    check("after tampering the endpoint says so", r["ok"] is False and r["broken_at"] == 2, r)
    o = world()
    check("another company's chain is not affected", client.get("/audit/chain/verify", headers=bearer(o["users"]["viewer"][0])).json()["ok"])


def main():
    for t in (test_append_only, test_chain_ok_and_isolated, test_tampering_detected, test_late_commit_never_skipped, test_concurrent_writers, test_api):
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(f"{t.__name__} ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
