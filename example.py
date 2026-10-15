"""Minimal illustration of a typed-reason guard with deterministic-key idempotency.

The 'database' is an in-memory list. The guard refuses raw inserts, requires a
reason from a closed enum, writes an audit record before the table write, and
skips inserts whose deterministic key already exists so that an operator retry
after a crash does not duplicate rows.

All data is synthetic.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import hashlib
import json
import pathlib
from typing import Iterable


# ---------------------------------------------------------------------------
# Closed enum of accepted reasons. Each member is documented in-code with
# why it is safe to accept. Adding a new member is an explicit code change.
# ---------------------------------------------------------------------------
class InsertReason(str, enum.Enum):
    # Canonical intake from an operator-authored list artefact.
    OPERATOR_LIST_INTAKE = "operator_list_intake"

    # Operator explicitly authorised a per-record force-add that normal
    # guardrails would have excluded. Requires a note in the audit log.
    OPERATOR_AUTHORIZED_FORCE_ADD = "operator_authorized_force_add"

    # Reinstating a record that was removed by an earlier cleanup pass
    # once the operator confirmed the removal was wrong.
    OPERATOR_RESTORE = "operator_restore"


# ---------------------------------------------------------------------------
# A pretend production table. Only the guard ever writes to this list.
# Direct append from the outside is considered a bug by convention; a code
# review check (not shown) would reject the pattern.
# ---------------------------------------------------------------------------
_TABLE: list[dict] = []
_AUDIT_LOG: list[dict] = []


# ---------------------------------------------------------------------------
# Deterministic key. Any two inserts that would be semantically the same
# (same operator, same target, same path, same value, same date) collapse
# to the same key.
# ---------------------------------------------------------------------------
def deterministic_key(
    *, operator_id: str, target_id: int, path: str, value: str, as_of: dt.date
) -> str:
    payload = json.dumps(
        {
            "operator_id": operator_id,
            "target_id": target_id,
            "path": path,
            "value": value,
            "as_of": as_of.isoformat(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# The guard itself. Three things must be true before anything else runs:
#   - reason is an InsertReason member
#   - target_ids is non-empty
#   - OPERATOR_AUTHORIZED_FORCE_ADD and OPERATOR_RESTORE require a note
# Then: write the audit record, compute the deterministic key, check for
# an existing row with that key, insert if absent.
# ---------------------------------------------------------------------------
class GuardError(RuntimeError):
    pass


@dataclasses.dataclass
class InsertResult:
    inserted: int
    skipped_idempotent: int
    audit_path: pathlib.Path | None


def guarded_insert(
    *,
    operator_id: str,
    target_ids: Iterable[int],
    path: str,
    value: str,
    as_of: dt.date,
    reason: InsertReason,
    note: str = "",
    audit_dir: pathlib.Path | None = None,
) -> InsertResult:
    if not isinstance(reason, InsertReason):
        raise GuardError(
            f"reason must be an InsertReason enum value, got {type(reason).__name__}"
        )
    target_ids = list(target_ids)
    if not target_ids:
        raise GuardError("target_ids must be non-empty")
    if (
        reason in (InsertReason.OPERATOR_AUTHORIZED_FORCE_ADD, InsertReason.OPERATOR_RESTORE)
        and not note
    ):
        raise GuardError(f"{reason.value} requires a note explaining the rationale")

    # Write the audit record BEFORE the table write, so a crash leaves a trail.
    audit_dir = audit_dir or pathlib.Path.cwd() / "audit"
    audit_dir.mkdir(exist_ok=True)
    audit_path = audit_dir / f"audit_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.json"
    audit_record = {
        "operator_id": operator_id,
        "target_ids": sorted(target_ids),
        "path": path,
        "value": value,
        "as_of": as_of.isoformat(),
        "reason": reason.value,
        "note": note,
    }
    audit_path.write_text(json.dumps(audit_record, indent=2))
    _AUDIT_LOG.append(audit_record)

    # Table write with idempotency check per target_id.
    inserted = 0
    skipped = 0
    existing_keys = {row["key"] for row in _TABLE}
    for tid in sorted(target_ids):
        key = deterministic_key(
            operator_id=operator_id, target_id=tid, path=path, value=value, as_of=as_of
        )
        if key in existing_keys:
            skipped += 1
            continue
        _TABLE.append(
            {
                "key": key,
                "operator_id": operator_id,
                "target_id": tid,
                "path": path,
                "value": value,
                "as_of": as_of.isoformat(),
                "reason": reason.value,
            }
        )
        existing_keys.add(key)
        inserted += 1

    return InsertResult(
        inserted=inserted, skipped_idempotent=skipped, audit_path=audit_path
    )


# ---------------------------------------------------------------------------
# Entry point: a short scenario that exercises the guard.
# ---------------------------------------------------------------------------
def main() -> None:
    print("=== 1. Normal batch insert ===")
    r = guarded_insert(
        operator_id="op-01",
        target_ids=[101, 102, 103],
        path="/status",
        value="active",
        as_of=dt.date(2026, 10, 15),
        reason=InsertReason.OPERATOR_LIST_INTAKE,
    )
    print(f"  inserted={r.inserted}  skipped_idempotent={r.skipped_idempotent}")

    print()
    print("=== 2. Same operator retries the same batch after a crash ===")
    r = guarded_insert(
        operator_id="op-01",
        target_ids=[101, 102, 103, 104],
        path="/status",
        value="active",
        as_of=dt.date(2026, 10, 15),
        reason=InsertReason.OPERATOR_LIST_INTAKE,
    )
    print(f"  inserted={r.inserted}  skipped_idempotent={r.skipped_idempotent}")
    print("  (101/102/103 were skipped as idempotent; 104 is new.)")

    print()
    print("=== 3. Guard refuses an insert without a reason ===")
    try:
        guarded_insert(
            operator_id="op-01",
            target_ids=[200],
            path="/status",
            value="active",
            as_of=dt.date(2026, 10, 15),
            reason="just do it",  # type: ignore[arg-type]
        )
    except GuardError as e:
        print(f"  rejected: {e}")

    print()
    print("=== 4. Guard refuses a force-add without a note ===")
    try:
        guarded_insert(
            operator_id="op-01",
            target_ids=[300],
            path="/status",
            value="active",
            as_of=dt.date(2026, 10, 15),
            reason=InsertReason.OPERATOR_AUTHORIZED_FORCE_ADD,
        )
    except GuardError as e:
        print(f"  rejected: {e}")

    print()
    print(f"table rows: {len(_TABLE)}")
    print(f"audit records: {len(_AUDIT_LOG)}")


if __name__ == "__main__":
    main()
