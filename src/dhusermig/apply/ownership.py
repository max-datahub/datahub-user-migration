from __future__ import annotations

from datahub.metadata.schema_classes import OwnerClass, OwnershipClass


def build_ownership_with_owner(
    ownership: OwnershipClass, new_urn: str, owner_types: list
) -> OwnershipClass:
    """owner_types is a list of (type, typeUrn) pairs -- typeUrn distinguishes
    CUSTOM owner types, so dedup must key on the full pair, not type alone."""
    owners = list(ownership.owners or [])
    existing = {(o.owner, o.type, getattr(o, "typeUrn", None)) for o in owners}
    for t, tu in owner_types:  # NO break — add EVERY captured (type, typeUrn) pair
        if (new_urn, t, tu) not in existing:
            owners.append(OwnerClass(owner=new_urn, type=t, typeUrn=tu))
            existing.add((new_urn, t, tu))
    kwargs: dict = {"owners": owners}
    if getattr(ownership, "lastModified", None) is not None:
        kwargs["lastModified"] = ownership.lastModified
    return OwnershipClass(**kwargs)


def build_ownership_without_owner(
    ownership: OwnershipClass, owner_urn: str
) -> OwnershipClass:
    if not ownership.owners:
        return ownership
    new_owners = [o for o in ownership.owners if o.owner != owner_urn]
    kwargs: dict = {"owners": new_owners}
    if getattr(ownership, "lastModified", None) is not None:
        kwargs["lastModified"] = ownership.lastModified
    return OwnershipClass(**kwargs)
