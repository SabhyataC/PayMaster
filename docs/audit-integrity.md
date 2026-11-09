# Audit log integrity

The audit log is the evidence for every decision and payment, so it is protected in three layers.

1. **Append-only.** Database triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log`. The only change ever allowed is the one-time chain seal below.
2. **Hash chain.** A background sealer (every 5 s, `AUDIT_SEAL_WORKER=0` turns it off) links each company's rows into a SHA-256 chain: `hash = sha256(previous hash | position | row contents)`. `audit_verify(company)` recomputes the whole chain. It detects an edited row, a removed row (middle or newest), a reordered row and a forged extra row. The `GET /audit/chain/verify` endpoint and the *Audit integrity* page run it.
3. **Separate runtime role (recommended for production).** Triggers stop the application. Someone who owns the table could disable them, and the chain would still catch that, but you can also take the ability away:

```sql
CREATE ROLE paymaster_app LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE paymaster TO paymaster_app;
GRANT USAGE ON SCHEMA public TO paymaster_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO paymaster_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM paymaster_app;
GRANT UPDATE (seal_seq, prev_hash, row_hash) ON audit_log TO paymaster_app;   -- the sealer
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO paymaster_app;
```
Run the app with `paymaster_app`; keep the owner role for migrations only.

**What it proves and what it does not.** Rows are chained shortly after they are written (normally within seconds). From then on any change is detectable. A row deleted in the first seconds, before it is sealed, is not. To protect against an owner who rewrites the whole chain, save the *head hash* shown on the Audit integrity page somewhere outside the database (an email, a ticket) and compare it later.

**Dev and test.** `python -m backend.seed`, `scripts/eval.py` and the test-set scripts wipe the log through `audit_chain.allow_reset`, which needs table ownership and is only used by those scripts.
