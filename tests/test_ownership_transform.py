from datahub.metadata.schema_classes import OwnershipClass, OwnerClass

from dhusermig.apply.ownership import (
    build_ownership_with_owner,
    build_ownership_without_owner,
)
from dhusermig.plan.builder import owner_types_for_user

OLD = "urn:li:corpuser:a@src.tld"
NEW = "urn:li:corpuser:a@dst.tld"


def _own(*types):
    return OwnershipClass(owners=[OwnerClass(owner=OLD, type=t) for t in types])


def test_capture_all_types_for_user():
    own = _own("BUSINESS_OWNER", "TECHNICAL_OWNER")
    assert owner_types_for_user(own, OLD) == [["BUSINESS_OWNER", None], ["TECHNICAL_OWNER", None]]


def test_add_owner_preserves_all_types():
    own = _own("BUSINESS_OWNER", "TECHNICAL_OWNER")
    types = owner_types_for_user(own, OLD)
    new_own = build_ownership_with_owner(own, NEW, types)
    new_entries = {(o.owner, o.type) for o in new_own.owners}
    # both old entries kept AND both new-user types added
    assert (NEW, "BUSINESS_OWNER") in new_entries
    assert (NEW, "TECHNICAL_OWNER") in new_entries
    assert (OLD, "BUSINESS_OWNER") in new_entries
    assert (OLD, "TECHNICAL_OWNER") in new_entries


def test_add_owner_is_idempotent():
    own = _own("BUSINESS_OWNER")
    once = build_ownership_with_owner(own, NEW, [["BUSINESS_OWNER", None]])
    twice = build_ownership_with_owner(once, NEW, [["BUSINESS_OWNER", None]])
    assert sum(1 for o in twice.owners if o.owner == NEW) == 1


def test_capture_distinct_custom_typeurns_not_collapsed():
    # Regression guard for the CUSTOM-type fix: two owners of type=CUSTOM with
    # distinct typeUrns must NOT collapse to one when deduped on type alone.
    own = OwnershipClass(
        owners=[
            OwnerClass(owner=OLD, type="CUSTOM", typeUrn="urn:li:ownershipType:steward"),
            OwnerClass(owner=OLD, type="CUSTOM", typeUrn="urn:li:ownershipType:approver"),
        ]
    )
    pairs = owner_types_for_user(own, OLD)
    # Discriminating: a type-only dedup would return a single ["CUSTOM", ...] pair.
    assert pairs == [
        ["CUSTOM", "urn:li:ownershipType:steward"],
        ["CUSTOM", "urn:li:ownershipType:approver"],
    ]

    new_own = build_ownership_with_owner(own, NEW, pairs)
    new_custom_entries = {(o.owner, o.type, o.typeUrn) for o in new_own.owners if o.owner == NEW}
    # Discriminating: dropping typeUrn when re-adding OwnerClass, or deduping the
    # append on (owner, type) only, would leave just one NEW/CUSTOM entry here.
    assert new_custom_entries == {
        (NEW, "CUSTOM", "urn:li:ownershipType:steward"),
        (NEW, "CUSTOM", "urn:li:ownershipType:approver"),
    }


def test_remove_owner_removes_all_types():
    keep = "urn:li:corpuser:keep@x.tld"
    own = OwnershipClass(
        owners=[
            OwnerClass(owner=OLD, type="BUSINESS_OWNER"),
            OwnerClass(owner=OLD, type="TECHNICAL_OWNER"),
            OwnerClass(owner=keep, type="TECHNICAL_OWNER"),
        ]
    )
    new_own = build_ownership_without_owner(own, OLD)
    assert not any(o.owner == OLD for o in new_own.owners)
    assert (keep, "TECHNICAL_OWNER") in {(o.owner, o.type) for o in new_own.owners}


def test_remove_owner_empty_owners_is_noop():
    own = OwnershipClass(owners=[])
    new_own = build_ownership_without_owner(own, OLD)
    assert not any(o.owner == OLD for o in (new_own.owners or []))
