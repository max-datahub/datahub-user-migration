from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from .schema import Change, Plan, State, UserMigration


def save_plan(plan: Plan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, indent=2)
    os.replace(tmp, path)


def load_plan(path: Path) -> Plan:
    with path.open(encoding="utf-8") as f:
        return Plan.from_dict(json.load(f))


def set_state(
    plan: Plan, change: Change, state: State, path: Path, error: str | None = None
) -> None:
    change.state = state
    change.error = error
    save_plan(plan, path)


def pending_changes(plan: Plan) -> Iterator[tuple[UserMigration, Change]]:
    """
    Yield (user, change) pairs still needing an apply attempt: PENDING (never
    attempted) or FAILED (attempted and failed -- retried on the next run, per
    the resumable-apply contract in README/RUNBOOK). Excludes DONE (already
    applied), SKIPPED, and INFO (detect-only, never dispatched).
    """
    for user in plan.users:
        for change in user.changes:
            if change.state in (State.PENDING, State.FAILED):
                yield user, change
