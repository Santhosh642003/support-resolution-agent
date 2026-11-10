# Failure analysis: defects found and fixed

A running log of real defects found during development, each with the test or check that exposed it. Entries are added as they happen; nothing here is hypothetical. All data in this project is simulated.

## F-001: confirmation-token expiry never fired inside a long transaction

- **Found:** 2026-11-10, task 3 (migrations), by a failing test.
- **Test:** `backend/tests/db/test_schema_guarantees.py::test_expired_token_is_rejected_by_update_and_by_trigger`. A token with a 1 ms lifetime was still consumable after `pg_sleep(0.05)`: `assert 1 == 0`.
- **Cause:** PostgreSQL's `now()` returns the transaction start time and is frozen for the transaction's life. Both the consume `UPDATE ... AND expires_at > now()` and the guard trigger compared against it, so inside any transaction that began before the expiry, an expired token looked valid. The execution path re-fetches state and re-runs rules inside one transaction, so this is reachable.
- **Fix:** the consume SQL and the guard trigger use `clock_timestamp()`, which advances during the transaction (migration 0002, `app/db/tokens.py`). Recorded as a standing convention in `CLAUDE.md`.
- **Class of bug:** time-of-check/time-of-use on an authorization boundary. The guarantee looked enforced and passed review; only a test that waited actually exercised it.

## F-002: literal percent signs broke the migration

- **Found:** 2026-11-10, task 3, on the first migration run (every database test errored in fixture setup).
- **Symptom:** `psycopg.ProgrammingError: incomplete placeholder: '%'`.
- **Cause:** the driver treats `%` as a parameter marker; the PL/pgSQL `RAISE EXCEPTION '% is append-only'` formats and `%ROWTYPE` contain literal percent signs.
- **Fix:** migrations 0002 and 0003 double `%` before executing the SQL. Covered by every database test, since the fixture migrates a fresh database.

## F-003: design gap, refunds were order-level only

- **Found:** 2026-11-10, task 3 review (PM review of the schema, before any rules engine existed).
- **Gap:** the first schema recorded refunds only per order. Three policy rules are item-level (proration by items shipped, accessed digital add-ons, `final_sale`), and a second refund request could not tell what had already been refunded per item.
- **Fix:** migration 0003 adds `refund_lines`, a per-item cumulative guard (`order_items_refunded_within_paid`), a commit-time header equals sum-of-lines check, and line-level drift detection. Tests in `backend/tests/db/test_refund_lines.py`.
- **Note:** this was caught by review, not by a test. It is listed because the cost of retrofitting after the rules engine would have been higher.

## F-004: a private key was committed by a blanket `git add -A`

- **Found:** 2026-11-10, by a pre-push scan of the full history (before anything was pushed).
- **What happened:** an SSH private key file (a cloud-instance key, placed in the repository folder by the project owner) was swept into a commit by `git add -A`. The commit was local only.
- **Containment:** the file was removed from the unpushed commit, the history was amended, `*.key`, `*.pem`, `ssh-key-*`, `id_rsa*` and `id_ed25519*` were added to `.gitignore`, and unreachable objects were purged. A rescan of all history found no key material, and the remote contains none. The file remains on disk, untracked and ignored. Because the key was never pushed, rotation was not needed.
- **Process failure to own:** the push command in the same step was not gated on the scan result; the scan had already come back clean on the corrected history, but the check should have been a precondition. Going forward, commits stage named paths, and the scan is run and read before any push.

## F-005: test expectations about TRUNCATE were wrong

- **Found:** 2026-11-10, task 3, while extending the append-only tests.
- **Symptom:** `TRUNCATE refunds` returned SQLSTATE 55006 (pending trigger events) and then 0A000 (table referenced by a foreign key) instead of the append-only trigger's `restrict_violation`.
- **Resolution:** both are native protections that still block the operation. The test now fires the deferred check first and uses `TRUNCATE ... CASCADE`, the case where only the `forbid_mutation` trigger stands in the way.
