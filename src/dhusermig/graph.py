from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Iterable, List, Optional, Set

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import GroupMembershipClass

from dhusermig.config import RunConfig

logger = logging.getLogger(__name__)

OWNED_BY_RELATIONSHIP = "OwnedBy"


class DiscoveryError(RuntimeError):
    """A plan collector could not complete; the plan would be silently incomplete,
    so plan building aborts."""

# GraphQL listSubscriptions (DataHub Cloud): filter by actorUrn in input
LIST_SUBSCRIPTIONS_QUERY = """
query listSubscriptions($input: ListSubscriptionsInput!) {
  listSubscriptions(input: $input) {
    total
    subscriptions {
      subscriptionUrn
    }
  }
}
"""


def get_graph(cfg: RunConfig) -> DataHubGraph:
    config = DatahubClientConfig(server=cfg.gms_url, token=cfg.token)
    return DataHubGraph(config)


def _get_related_entities_with_count(
    graph: DataHubGraph,
    entity_urn: str,
    relationship_types: List[str],
    direction: DataHubGraph.RelationshipDirection,
    page_size: int = 500,
) -> Iterable[str]:
    """
    Get URNs of related entities, requesting up to page_size per request so we
    fetch all pages (some backends default to a small count and only return one page).
    Yields entity URNs (related.urn).
    """
    get_generic = getattr(graph, "_get_generic", None)
    endpoint = getattr(graph, "_relationships_endpoint", None)
    if not get_generic or not endpoint:
        for related in graph.get_related_entities(
            entity_urn=entity_urn,
            relationship_types=relationship_types,
            direction=direction,
        ):
            yield related.urn
        return
    start = 0
    while True:
        params: dict = {
            "urn": entity_urn,
            "direction": direction.value,
            "relationshipTypes": relationship_types,
            "start": start,
            "count": page_size,
        }
        response = get_generic(url=endpoint, params=params)
        entities = response.get("entities", [])
        for item in entities:
            yield item.get("urn") or item.get("entity", {}).get("urn")
        n = len(entities)
        if n == 0:
            break
        start += n
        if n < page_size:
            break


def _get_entity_urns_owned_by_via_search(
    graph: DataHubGraph,
    owner_urn: str,
) -> Set[str]:
    """
    Find entity URNs that have owner_urn in their ownership aspect via search API.
    An empty set without errors is a legitimate result; if search is unavailable
    or every search field errors, raises DiscoveryError (the caller decides
    whether a relationship-walk result can still cover for it).
    """
    result: Set[str] = set()
    get_urns = getattr(graph, "get_urns_by_filter", None)
    if not get_urns:
        raise DiscoveryError("search-by-owner unavailable (graph has no get_urns_by_filter)")
    errors: list = []
    for field in ("owners", "ownerUrns"):
        try:
            for urn in get_urns(
                query="*",
                extraFilters=[{"field": field, "condition": "EQUAL", "values": [owner_urn]}],  # noqa: N806
            ):
                if urn:
                    result.add(urn)
            if result:
                break
        except Exception as e:
            logger.debug("Search by owner field %r failed: %s", field, e)
            errors.append(e)
    if len(errors) == 2:
        raise DiscoveryError(
            f"search-by-owner failed for all fields for {owner_urn}: {errors}"
        )
    return result


def get_entity_urns_owned_by_user(
    graph: DataHubGraph,
    user_urn: str,
) -> List[str]:
    """
    Return all entity URNs that have user_urn as owner.

    Uses (1) relationship API (OwnedBy edges, paginated) then (2) search/scroll
    (filter by owners or ownerUrns). Either path may fail on its own (a warning
    is logged and the other path's result is used); if BOTH fail we cannot know
    what the user owns, so DiscoveryError aborts the plan build.
    """
    entity_urns: Set[str] = set()
    relationship_error: Optional[Exception] = None
    try:
        for urn in _get_related_entities_with_count(
            graph,
            entity_urn=user_urn,
            relationship_types=[OWNED_BY_RELATIONSHIP],
            direction=DataHubGraph.RelationshipDirection.INCOMING,
            page_size=500,
        ):
            if urn:
                entity_urns.add(urn)
    except Exception as e:
        relationship_error = e
        logger.warning(
            "Relationship API failed for owner %s (e.g. 500 when offset >= 10k); falling back to search: %s",
            user_urn,
            e,
        )
    try:
        entity_urns |= _get_entity_urns_owned_by_via_search(graph, user_urn)
    except Exception as search_error:
        if relationship_error is not None:
            raise DiscoveryError(
                f"Ownership discovery failed for {user_urn}: relationship walk failed "
                f"({relationship_error}) and search fallback failed ({search_error})"
            ) from search_error
        logger.warning(
            "Search-by-owner failed for %s; proceeding with relationship results only: %s",
            user_urn,
            search_error,
        )
    return list(entity_urns)


def _get_native_group_membership_class() -> Optional[type]:
    """Return NativeGroupMembershipClass if available."""
    try:
        from datahub.metadata.schema_classes import NativeGroupMembershipClass
        return NativeGroupMembershipClass
    except ImportError:
        return None


def get_group_urns_for_user(
    graph: DataHubGraph,
    user_urn: str,
    page_size: int = 100,
) -> List[str]:
    """
    Return group URNs the user is a member of (each group at most once).
    Reads groupMembership and nativeGroupMembership aspects on the corpuser; these are
    the source of truth and do not depend on the relationship index.
    Deduplicates since a group can appear in both aspects.
    """
    group_urns: Set[str] = set()
    membership = graph.get_aspect(user_urn, GroupMembershipClass)
    if membership and getattr(membership, "groups", None):
        group_urns.update(membership.groups)
    native_cls = _get_native_group_membership_class()
    if native_cls:
        native_membership = graph.get_aspect(user_urn, native_cls)
        if native_membership and getattr(native_membership, "nativeGroups", None):
            group_urns.update(native_membership.nativeGroups)
    return list(group_urns)


def _get_subscription_urns_for_user_via_graphql(
    graph: DataHubGraph,
    user_urn: str,
    page_size: int = 100,
) -> List[str]:
    """
    Return subscription URNs for user_urn via GraphQL listSubscriptions(actorUrn).
    Used when the UI/Cloud API supports ListSubscriptionsInput.actorUrn.
    Returns [] if GraphQL is not available or does not support actorUrn filter
    (unsupported response shape, e.g. non-Cloud); query errors propagate so the
    caller can decide whether the fallback covers for them.
    """
    urns: List[str] = []
    start = 0
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        return []
    while True:
        variables = {
            "input": {
                "actorUrn": user_urn,
                "start": start,
                "count": page_size,
            }
        }
        result = execute(
            LIST_SUBSCRIPTIONS_QUERY,
            variables=variables,
            operation_name="listSubscriptions",
        )
        data = (result or {}).get("data") or result or {}
        list_data = data.get("listSubscriptions") if isinstance(data, dict) else None
        if not list_data or not isinstance(list_data, dict):
            break
        subs = list_data.get("subscriptions") or []
        for sub in subs:
            if isinstance(sub, dict) and sub.get("subscriptionUrn"):
                urns.append(str(sub["subscriptionUrn"]))
        if len(subs) < page_size:
            break
        start += page_size
    return urns


def get_subscription_urns_for_user(
    graph: DataHubGraph,
    user_urn: str,
    batch_size: int = 50,
) -> List[str]:
    """
    Return subscription entity URNs for user_urn.
    Tries GraphQL listSubscriptions(actorUrn) first (DataHub Cloud); falls back to
    listing subscription entities and checking subscriptionInfo.actorUrn (OSS / entity API).
    If the fallback also fails, raises DiscoveryError -- the plan would silently
    miss subscriptions otherwise.
    """
    try:
        urns = _get_subscription_urns_for_user_via_graphql(graph, user_urn, page_size=batch_size)
        if urns:
            return urns
    except Exception as e:
        logger.warning("listSubscriptions failed; falling back to entity listing: %s", e)
    SubscriptionInfoClass = getattr(
        __import__("datahub.metadata.schema_classes", fromlist=["SubscriptionInfoClass"]),
        "SubscriptionInfoClass",
        None,
    )
    if SubscriptionInfoClass is None:
        logger.warning(
            "SubscriptionInfoClass unavailable in the installed SDK; subscription discovery skipped"
        )
        return []
    out: List[str] = []
    start = 0
    try:
        while True:
            batch = graph.list_all_entity_urns("subscription", start=start, count=batch_size)
            if not batch:
                break
            for sub_urn in batch:
                info = graph.get_aspect(sub_urn, SubscriptionInfoClass)
                if info and getattr(info, "actorUrn", None) == user_urn:
                    out.append(sub_urn)
            if len(batch) < batch_size:
                break
            start += batch_size
    except Exception as e:
        raise DiscoveryError(f"Subscription discovery failed for {user_urn}: {e}") from e
    return out


def get_entity_via_api(
    gms_url: str,
    token: Optional[str],
    urn: str,
) -> Optional[dict]:
    """GET full entity via OpenAPI v3 entity generic. Returns response dict or None."""
    base = gms_url.rstrip("/")
    encoded_urn = urllib.parse.quote(urn, safe="")
    url = f"{base}/openapi/v3/entity/generic/{encoded_urn}?systemMetadata=false"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("GET entity failed for %s: %s", urn, e)
        return None


def user_exists_via_api(gms_url: str, token: Optional[str], user_urn: str) -> bool:
    """
    Return True if the user entity can be fetched via the same OpenAPI used for backups.
    Use this for validation so we fail if the user doesn't exist (same as backup would fail).
    """
    return get_entity_via_api(gms_url, token, user_urn) is not None
