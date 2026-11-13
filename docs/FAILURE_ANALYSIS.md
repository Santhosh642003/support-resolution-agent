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

## F-006: tests shared one database and read whole tables, so another test's committed rows broke the assertions

- **Found:** 2026-11-11, task 5 (seed data), by three failing tests on the first full-suite run.
- **Tests:** `backend/tests/db/test_seed.py::test_cross_customer_ownership_holds_for_every_seeded_row`, `::test_no_refunds_are_seeded_and_the_drift_assertion_passes`, `::test_seed_text_is_labeled_simulated_in_the_database`. They passed when run alone and failed in the full suite, e.g. an "unlabeled" customer named `Race` in a query that was meant to see only seeded rows.
- **Cause:** the database fixtures give each test a rolled-back transaction, but the concurrency tests (`test_concurrent_refunds.py`) must commit, because a real race needs two real connections. Their rows (customers, orders, refunds) stay in the session-scoped test database. My new tests ran `SELECT ... FROM customers`, `FROM refunds` and `FROM orders` with no filter, so they also read those rows. The seed itself was correct.
- **Fix:** assertions about the seed are scoped to the seeded customers' ids (`_seed_customer_ids()`); they never read a whole table.
- **Why this is logged although the seed was fine:** the failure was visible only because the polluting rows happened to violate the assertion. The same pollution can make an assertion *pass* wrongly: `count(*) = 0` over an unfiltered table is true only if no earlier test committed there, and `== N` is true only if none added more. Such a test is green for the wrong reason and goes red or green depending on test order. It will recur as the suite grows.
- **Class of bug:** shared mutable test state; order-dependent tests; false confidence from a green result that does not depend on the thing under test.
- **Rule going forward:** any test that asserts on a table's contents scopes the query to ids it created or seeded. Tests that commit (concurrency) are the only polluters and are the thing to fix first if this recurs, by cleaning up after themselves or using their own database.
- **The first fix was insufficient (recurred 2026-11-13, task 8).** Fixing the three named tests and writing a rule did not stop the pattern: while writing the order-status workflow tests I typed `SELECT count(*) FROM actions` and it failed under the full suite for the same reason, because the unscoped query is the obvious thing to type and the rule lives only in a document. A convention that depends on remembering is not a control.
- **What replaced it:** (1) `backend/tests/db/scoped.py`: `count_rows`, `sum_column` and `fetch_rows` take a mandatory `Scope` (a column and at least one value; an empty scope and non-identifier names raise), so there is no way to call them without saying whose rows the assertion is about and no 'everything' scope. (2) `backend/tests/unit/test_no_unscoped_aggregates.py`: an AST scan of every test file that fails on any SQL string with an aggregate and a FROM but no WHERE (f-strings re-joined; a short allowlist for catalog and policy reference tables, each with its reason), so writing an unscoped assertion by hand fails CI. The existing tests were migrated to the helpers, and the checker has its own tests for the unsafe and safe forms. The unsafe pattern now takes deliberate effort; the safe one is the shorter thing to type.

## F-007: an approximate vector index made retrieval depend on database state

- **Found:** 2026-11-12, task 7C, by `backend/tests/db/test_retrieval_recall.py::test_the_recorded_recall_numbers_reproduce_exactly`. It passed when run alone and failed inside the full suite: vector-only full recall on one configuration was 21 in the test database and 22 in the recorded file.
- **Cause:** the vector leg orders by `embedding <=> query`, which the planner may serve from the approximate HNSW index or from an exact scan, depending on table statistics. The statistics differ between a freshly ingested database and one that has seen many rolled-back ingestions, so the same query could return a different top-k. The recorded numbers came from a database where the planner chose the exact scan; the full suite's database got the approximate index.
- **Impact:** retrieval results were not a pure function of the query, the date and the corpus. The recorded recall numbers were still correct (the exact scan reproduces them byte for byte), but production retrieval could have differed from them.
- **Fix:** the vector leg now disables index scans for its statement, so the ranking is an exact scan; at tens to hundreds of clauses an approximate index buys nothing. `test_the_vector_leg_is_exact_so_results_do_not_depend_on_planner_state` compares the leg with a brute-force cosine ranking and checks the setting. Revisit, and measure, only when the corpus is large enough for an ANN index to matter.
- **Class of bug:** nondeterminism from an approximate index and planner state; a result that looked stable because the test was run in isolation. The test that exposed it was the one asserting a number is reproducible, which is why that assertion exists.

## F-008: a pre-registered decision rule was evaluated at a saturated point

- **Found:** 2026-11-12, task 7D, when the registered fusion rule returned an outcome the rest of the same results contradicted.
- **Evidence:** `backend/tests/db/test_retrieval_recall_v2.py::test_the_registered_fusion_rule_decided_on_a_saturated_comparison`. Rule R2 compared hybrid with vector-only on selection full recall at a single k (7). Under the structured scope every candidate except full-text-only scores 16 of 16 there, so "strictly beats vector-only" could not be satisfied and the rule fell through to its tie-break, choosing vector-only. At the unsaturated k of 3 to 6 on the same set, hybrid and full-text-only are at least as good as vector-only at every k.
- **Cause:** I fixed the comparison point before measuring and picked one where the structured scope (in-scope sets of 2 to 14 clauses) makes the metric ceiling out. A decision rule is only as good as its power to discriminate, and I did not check that before registering it.
- **What was done:** the rule's tie-break carried no information, so no change was made: the existing default was kept because the evidence did not separate the systems, and the decision is deferred to M3 with a rule that compares across several k. The saturation is pinned in a test so it cannot be tidied away. Defaults for the choices the rule decided validly (the lexical configuration, k) were applied; the rule's fusion output was neither applied nor overridden.
- **Rule going forward:** a registered selection rule compares at several values (or over a range) and states what it does when the metric saturates, before the data is seen. Pre-registration protects against choosing on the held-out set; it does not make a badly designed rule correct, and it must not be used as cover for one.
- **Class of bug:** methodology defect (ceiling effect in a selection rule), found by the data contradicting the rule's own conclusion.

## F-009: the explanation promised follow-up the system cannot perform

- **Found:** 2026-11-13, by reading the first real model output (task 8, step 5, live Groq run, `openai/gpt-oss-120b`), not by any test. The verifier passed all three replies.
- **What was said to the customer**, verbatim, in three of three live conversations:
  1. SIM-2003, on schedule: "We’ll keep monitoring its progress and let you know if anything changes."
  2. SIM-2002, due today: "We’ll keep monitoring it and let you know if anything changes."
  3. SIM-2001, presumed lost, with an injection attempt in the message: "We are sorry for the inconvenience and will work with you to resolve this."
- **Why it is a defect, not a blemish:** the system has no monitoring, no notification and no follow-up in this milestone. The first two are fabricated follow-up and the third is an open-ended promise. A support agent that invents commitments is worse than one that says nothing, because the customer then waits for something that will never happen.
- **Why every check passed:** `verify_explanation` compared the model's outcome, its cited clause keys, the dates and the absence of money amounts with the determination. None of the three sentences contains an outcome, a key, a date or an amount. This is the gap SPEC 10.5 already recorded as residual risk ("free prose can still say something misleading"); it had been written down as a measurement for M3 and was in fact already a live defect.
- **Cause:** the model was asked to write a customer-facing explanation and the closing sentence of a support message is, by habit, a next step. The prompt did not forbid it, and what happens next is a fact about the system that the model cannot know.
- **Fix (same shape as the citation fix: take the decision away from the model):**
  1. The model writes the explanatory body only. The prompt now forbids next steps, promises and anything the company or assistant will do (`explain-2`).
  2. What happens next is chosen by code from the outcome and from whether a customer-facing handoff was raised (`templates.closing_line`): "this demo does not send automatic updates" for the outcomes where the order is still in motion, "no refund, replacement or other change has been made" for presumed lost, nothing for the outcomes where nothing further happens.
  3. A check over the model's body for commitment phrasing (`explanation.COMMITMENT_PATTERNS`) **fails the turn closed**, with the phrases recorded in the trace (`commitment_phrases`), so the rate can be counted. Fail closed rather than strip was chosen for development so the rate is visible and model text is never silently edited; to be revisited before M3.
- **Tests:** `tests/unit/test_commitment_check.py` (the three sentences above verbatim, other named phrasings, factual sentences not flagged, the closing line per outcome never matching the check itself), and in `tests/db/test_order_status_workflow.py` a promise that is otherwise correct fails closed end to end, and a verified reply ends with exactly one deterministic line.
- **Known limits (stated, not hidden):** the check catches a named class of phrasing and cannot catch paraphrase. `test_a_paraphrase_is_not_caught_and_that_is_the_stated_limit` records a promise-like sentence that passes. The residual rate is measured in M3 under the citation-support rubric as its own category, "unbacked commitment", counted separately from citation errors. After the fix, four live conversations produced no commitment phrasing (n=4; evidence the prompt works, not a rate).
- **Class of bug:** a verifier that checks what it was written to check and nothing else; a model allowed to state facts about the system's own future behaviour.

## F-010: a security control silently disabled a correctness control (RLS vs. the drift views)

- **Found:** M2 task 0 (row-level security, SPEC 17), while building it; entered in this log on the PM's review of that task.
- **Symptom:** after `FORCE ROW LEVEL SECURITY` was set, the refund drift views reported no drift to a superuser.
- **Tests:** `tests/db/test_refund_lines.py::test_without_security_invoker_row_level_security_hides_drift_from_the_check` reproduces it (the option removed inside a rolled-back transaction: real drift, empty result) and `::test_every_view_in_the_schema_is_security_invoker` fails for any future view without the option.
- **Cause:** a view runs with its OWNER's privileges by default. The owner (`support_owner`) is subject to FORCE RLS, and with no customer bound it sees no rows, so the view evaluated over an empty table set no matter who queried it. The invariant check kept running and kept answering "no drift".
- **Fix:** both views are `security_invoker` (migration 0012), so they evaluate as the caller: the superuser audit sees every row, the app role sees only its customer.
- **Why it matters:** an invariant checker that cannot see violations is worse than none, because it reports success. Nothing errored, nothing was logged, and every existing test passed; only a test that created a violation and asked the view for it could notice.
- **Rule going forward:** every view over a protected table is `security_invoker`, and a correctness check is itself tested by feeding it a violation (the drift tests do). Adding RLS to a table changes what every view, function and report that reads it can see; list them when you add the policy.
- **Class of bug:** one control silently defeating another; a checker that fails open.

