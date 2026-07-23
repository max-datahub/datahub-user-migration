# src/dhusermig/verify.py
from __future__ import annotations

from typing import List

from datahub.ingestion.graph.client import DataHubGraph


def _owners_of(graph: DataHubGraph, entity_urn: str) -> set:
    """
    Return the set of owner URNs currently on entity_urn's Ownership aspect.
    Reads get_ownership (primary storage), not a search/relationship index --
    so this can't produce a false pass/fail from index lag right after a write.
    """
    ownership = graph.get_ownership(entity_urn)
    return {o.owner for o in (ownership.owners or [])} if ownership else set()


def missing_expected_ownership(
    graph: DataHubGraph,
    new_urn: str,
    expected_targets: List[str],
) -> List[str]:
    """
    Return the subset of expected_targets whose Ownership aspect does NOT list
    new_urn as an owner (empty list = verification passed).

    Used for the migrate phase: migration ADDS the new owner without removing
    the old one, so the meaningful signal is "does the new user own everything
    the plan intended to add", not "is the old owner gone" (that's cleanup's
    check, see remaining_old_ownership). Checks one target at a time via
    get_ownership -- the plan already tells us exactly which targets matter,
    so there's no need to enumerate a user's ownership via search.
    """
    return [target for target in expected_targets if new_urn not in _owners_of(graph, target)]


def remaining_old_ownership(
    graph: DataHubGraph,
    old_urn: str,
    targets: List[str],
) -> List[str]:
    """
    Return the subset of targets whose Ownership aspect STILL lists old_urn as
    an owner (empty list = cleanup verification passed). Same get_ownership-based,
    plan-target-driven lookup as missing_expected_ownership, just checking
    presence instead of absence.
    """
    return [target for target in targets if old_urn in _owners_of(graph, target)]


def compare_counts(before: dict, after: dict) -> List[str]:
    """Pure comparison of per-kind counts for old vs new user."""
    msgs: List[str] = []
    if before.get("ownership") != after.get("ownership"):
        only_b = set(before.get("ownership", set())) - set(after.get("ownership", set()))
        only_a = set(after.get("ownership", set())) - set(before.get("ownership", set()))
        if only_b:
            msgs.append(f"ownership missing after: {len(only_b)}")
        if only_a:
            msgs.append(f"ownership extra after: {len(only_a)}")
    for k in ("subscription", "group_membership"):
        if before.get(k, 0) != after.get(k, 0):
            msgs.append(f"{k} count mismatch: before={before.get(k)}, after={after.get(k)}")
    return msgs
