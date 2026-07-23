from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class ChangeKind(str, Enum):
    CREATE_USER = "CREATE_USER"
    ADD_OWNERSHIP = "ADD_OWNERSHIP"
    COPY_SUBSCRIPTION = "COPY_SUBSCRIPTION"
    REWRITE_POLICY = "REWRITE_POLICY"
    MIGRATE_VIEW = "MIGRATE_VIEW"
    DETECT_TOKEN = "DETECT_TOKEN"
    DETECT_HOMEPAGE = "DETECT_HOMEPAGE"
    DETECT_RECREATION_SOURCE = "DETECT_RECREATION_SOURCE"
    REMOVE_OWNERSHIP = "REMOVE_OWNERSHIP"
    DELETE_SUBSCRIPTION = "DELETE_SUBSCRIPTION"
    DELETE_USER = "DELETE_USER"
    REINDEX_USER = "REINDEX_USER"
    REMOVE_POLICY_ACTOR = "REMOVE_POLICY_ACTOR"


class State(str, Enum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INFO = "INFO"


@dataclass
class Change:
    kind: ChangeKind
    target: str
    state: State = State.PENDING
    # (type, typeUrn) pairs, e.g. [["CUSTOM", "urn:li:ownershipType:steward"], ...].
    # typeUrn is null for built-in enum types. A pair per distinct (type, typeUrn)
    # combo -- collapsing on type alone would drop distinct CUSTOM typeUrns.
    owner_types: list[list[str | None]] = field(default_factory=list)
    source: str | None = None
    entity: str | None = None
    field: str | None = None
    note: str | None = None
    error: str | None = None


@dataclass
class UserMigration:
    old_email: str
    old_urn: str
    new_email: str
    new_urn: str
    changes: list[Change] = field(default_factory=list)


@dataclass
class PlanMeta:
    tool_version: str
    created_at: str
    gms_url_fingerprint: str
    target_domain: str
    phase: str
    options: dict = field(default_factory=dict)


@dataclass
class Plan:
    meta: PlanMeta
    users: list[UserMigration] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        meta = PlanMeta(**d["meta"])
        users = [
            UserMigration(
                old_email=u["old_email"], old_urn=u["old_urn"],
                new_email=u["new_email"], new_urn=u["new_urn"],
                changes=[
                    Change(
                        kind=ChangeKind(c["kind"]),
                        target=c["target"],
                        state=State(c.get("state", "PENDING")),
                        owner_types=[list(pair) for pair in (c.get("owner_types") or [])],
                        source=c.get("source"),
                        entity=c.get("entity"),
                        field=c.get("field"),
                        note=c.get("note"),
                        error=c.get("error"),
                    )
                    for c in u.get("changes", [])
                ],
            )
            for u in d.get("users", [])
        ]
        return cls(meta=meta, users=users)
