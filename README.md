# Safe mutation patterns

Design notes on writing to a shared production table when you cannot add database constraints and cannot change the schema. Written up from field experience building an operator-driven pipeline whose write path depends on a table owned by another team, with no option to migrate.

## 1. A typed-reason guard around every mutation

The baseline problem is that `INSERT` and `DELETE` against the shared table are called from several code paths. Any of them can get it wrong, and once the data lands, undoing it costs more than preventing it.

The pattern:

- Raw SQL against the table is refused by convention and by code. A linter check rejects the string pattern; the connection factory refuses the statement at runtime.
- Every mutation goes through a helper whose signature requires a `reason`, which is a value of a closed enum. There is no free-text escape hatch.
- Each enum member documents, in-code, why it is safe to accept as a reason. Adding a new member requires sign-off and a design-doc entry; the enum does not grow silently.
- Before the DB roundtrip, the mutation is written to a local audit record. If the write crashes mid-call, the audit record is the only ground truth left for recounts.

Why audit-first rather than audit-after: a crash between the DB write and the audit write leaves a mutation with no provenance. A crash between the audit write and the DB write leaves an audit record that reports a mutation that did not happen. The second failure mode is easier to detect and reconcile than the first.

## 2. Idempotency without a schema migration

The retry problem is simple. An operator-driven step can crash halfway through a batch. The operator retries. The same records get written twice. The patches are no-ops, but the audit trail is now ambiguous: a reviewer cannot tell "legitimate retry" from "bug writing twice".

Three options were on the table.

**Option A — add a database-level `UNIQUE` constraint.** Correct and clean. Blocked in practice because the migration lives in another team's ownership and takes a long roundtrip to approve.

**Option B — query the JSON column before every insert** to check whether an equivalent record already exists. Works but is slow: the JSON column has no index that matches this lookup, and at batch scale the pre-check becomes the dominant cost.

**Option C — stamp a deterministic key into two existing columns that already have a partial index on them.** Compute a hash of the fields that make a mutation unique for retry purposes (operator identity, target record, path, value, date). Write it to those two columns on every insert. Before inserting, check whether a row with that same key already exists; if it does, increment a `skipped_idempotent` counter so a human reader can tell retries from new work.

C was picked. It uses indices and columns that already exist, needs no migration, and does not require coordination with another team.

The honest limit: `SELECT`-then-`INSERT` is not atomic. Two concurrent processes with the same operator identity could still both insert. That is acceptable because the retry pattern is serial — the same operator trying again after a crash. If the system ever ran concurrently against the same operator identity, the right fix is Option A.

## 3. Per-query-class timeouts

The failure mode: a long-running query against a very large table, running on behalf of an automated agent, was issued without a timeout. The client disconnected but the query kept running on the server. It consumed enough resources that the rest of the workload was starved for several minutes.

The obvious response is a single global timeout at the database side. That gets rejected because the same codebase makes queries with legitimately different cost profiles. An indexed lookup should fail fast within a few seconds. A spatial query over a modest region needs tens of seconds. A full-table aggregate for an end-of-batch report can take minutes. One number cannot bound all three safely.

The pattern that worked:

- The connection factory requires a `timeout_class` argument at call time.
- The classes are named by what the query is doing, not by magnitude. Four classes cover the real workload, each with a documented default and a one-line note on what each one is for:

    - `lookup` — 15s. Indexed by place-id or chain-id.
    - `spatial` — 60s. ST_DWithin, bounding-box intersections.
    - `aggregate` — 120s. COUNT, GROUP BY on large tables.
    - `heavy` — 300s. ILIKE on name, JSONB field filters.
- `SET statement_timeout = ...` runs before the first query on each connection, derived from the class.
- The factory refuses to build a connection with no class, or with `timeout_class=heavy` from a code path that is not whitelisted. This forces the author of a new call site to think about the class rather than reaching for the biggest one.
- A unit test asserts that the connection object cannot be built without a class. A smoke test runs an intentionally-slow query against a non-production endpoint and verifies that it is cancelled at the expected limit.

The point of naming the classes rather than exposing a raw seconds argument is that `timeout_class="spatial"` reads as a design decision in a code review, while `timeout_seconds=60` reads as a magic number.

## 4. When a service does not do what the team assumes

A duplicate-merge service was understood by the team to update both the main record and a secondary join table that downstream code reads. Row counts drifted after merges in a way that should not have been possible if that assumption held.

Investigation showed the service was updating the main records correctly but was not cascading the change to the join table. No one on either side had documented the behaviour either way.

The reflex is to ask the service owner to add the cascade. That got rejected because it was a long roundtrip and because the join-table cleanup is not what the merge service is for. It belongs in the step that produced the join-table rows in the first place.

The fix was a scoped `DELETE` run after every merge batch: remove rows from the join table that point at any now-merged child record, restricted to the batch's identifiers so it cannot over-delete. The fix was written as a reusable helper, called from the pipeline step that produced the rows, and the gap in the service's behaviour was documented explicitly in the decision log so the next person does not relearn it the hard way.

The point that matters more than the fix: documenting an assumption that turned out to be false is as important as fixing it. A codebase that silently works around a service's quirks accumulates expensive tribal knowledge. Writing down what the service actually does, with a dated note, turns the quirk into something a reader can plan around.

## Example

[`example.py`](example.py) implements the two patterns from sections 1 and 2 end-to-end on a synthetic in-memory table. It exposes a `guarded_insert` that requires a reason from a closed enum and writes an audit record before touching the "database"; attempts to insert the same logical mutation twice are detected by the deterministic key and counted as idempotent skips rather than duplicate writes.
