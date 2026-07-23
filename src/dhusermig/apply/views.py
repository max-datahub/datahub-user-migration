from __future__ import annotations

import logging
from typing import List

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.metadata.schema_classes import (
    CorpUserSettingsClass,
    DataHubViewInfoClass,
    OwnershipClass,
    SystemMetadataClass,
)

from dhusermig.apply.ownership import build_ownership_with_owner, build_ownership_without_owner
from dhusermig.graph import DiscoveryError
from dhusermig.plan.builder import owner_types_for_user

logger = logging.getLogger(__name__)

# GraphQL EntityType enum value for dataHubView. NOTE: the Python SDK's
# get_urns_by_filter maps the entity name "dataHubView" to an invalid enum
# ("VIEW"), which raises at query time -- so we query scrollAcrossEntities with
# the correct enum directly instead of going through get_urns_by_filter.
VIEW_ENTITY_TYPE_ENUM = "DATAHUB_VIEW"

_SCROLL_VIEWS_QUERY = """
query scrollViews($types: [EntityType!], $count: Int!, $scrollId: String) {
  scrollAcrossEntities(input: {types: $types, query: "*", count: $count, scrollId: $scrollId}) {
    nextScrollId
    searchResults { entity { urn } }
  }
}
"""


def personal_views_for_user(graph: DataHubGraph, user_urn: str) -> List[str]:
    """
    Return URNs of PERSONAL dataHubView entities created by user_urn.

    dataHubView has no Ownership aspect (entity-registry.yml lists only
    dataHubViewInfo); the owner signal is created.actor. The listMy/GlobalViews
    GraphQL queries only list the CURRENT actor's views, not an arbitrary
    user's, so this scrolls all dataHubView entities (via scrollAcrossEntities
    with the DATAHUB_VIEW enum) and filters to PERSONAL views created by
    user_urn using the dataHubViewInfo aspect -- source of truth, no dependency
    on Elasticsearch field names. Detect-only. Raises DiscoveryError if
    search/GraphQL errors -- the plan would silently miss views otherwise.
    """
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        logger.warning("GraphQL unavailable; personal-view discovery skipped")
        return []
    result: List[str] = []
    scroll_id = None
    try:
        while True:
            data = execute(
                _SCROLL_VIEWS_QUERY,
                variables={
                    "types": [VIEW_ENTITY_TYPE_ENUM],
                    "count": 200,
                    "scrollId": scroll_id,
                },
            )
            scroll = (data or {}).get("scrollAcrossEntities") or {}
            for hit in scroll.get("searchResults") or []:
                urn = (hit.get("entity") or {}).get("urn")
                if not urn:
                    continue
                info = graph.get_aspect(urn, DataHubViewInfoClass)
                created = getattr(info, "created", None) if info else None
                if (
                    info is not None
                    and str(getattr(info, "type", "")) == "PERSONAL"
                    and getattr(created, "actor", None) == user_urn
                ):
                    result.append(urn)
            scroll_id = scroll.get("nextScrollId")
            if not scroll_id:
                break
    except Exception as e:
        raise DiscoveryError(f"Personal-view discovery failed for {user_urn}: {e}") from e
    return result


def build_view_ownership_repoint(ownership: OwnershipClass, old_urn: str, new_urn: str) -> OwnershipClass:
    types = owner_types_for_user(ownership, old_urn) or [["TECHNICAL_OWNER", None]]
    added = build_ownership_with_owner(ownership, new_urn, types)
    return build_ownership_without_owner(added, old_urn)


def migrate_view(
    graph: DataHubGraph,
    view_urn: str,
    old_urn: str,
    new_urn: str,
    dry_run: bool = False,
) -> bool:
    """
    Re-point a view's Ownership aspect from old_urn to new_urn. Best-effort:
    dataHubView is not currently registered with an Ownership aspect (see
    entity-registry.yml), so on a live instance this is a no-op today (returns
    False -- no aspect to migrate) unless/until one is added. Personal-view
    migration is therefore detect-only in v1: an operator must recreate the
    view under the new user rather than rely on an automated re-point. Never
    raises -- the caller downgrades a False return to an INFO-level finding.
    """
    try:
        ownership = graph.get_aspect(view_urn, OwnershipClass)
        if not ownership or not ownership.owners:
            return False
        new_ownership = build_view_ownership_repoint(ownership, old_urn, new_urn)
        if dry_run:
            logger.info("Dry run: would repoint ownership on view %s", view_urn)
            return True
        mcp = MetadataChangeProposalWrapper(
            entityUrn=view_urn,
            aspect=new_ownership,
            systemMetadata=SystemMetadataClass(runId="user-migration"),
        )
        graph.emit_mcp(mcp)
        return True
    except Exception as e:
        logger.warning("Failed to migrate view %s: %s", view_urn, e)
        return False


def homepage_findings_for_user(graph: DataHubGraph, user_urn: str) -> List[str]:
    """
    Detect personalized homepage/pinned-module state from corpUserSettings
    (homePage.pageTemplate, homePage.dismissedAnnouncements, views.defaultView --
    see CorpUserSettings.pdl). Report-only: returns human-readable note strings;
    the collector records these as DETECT_HOMEPAGE INFO changes. Raises
    DiscoveryError if the aspect fetch errors (a missing aspect is benign).
    """
    notes: List[str] = []
    try:
        settings = graph.get_aspect(user_urn, CorpUserSettingsClass)
    except Exception as e:
        raise DiscoveryError(f"Homepage discovery failed for {user_urn}: {e}") from e
    if not settings:
        return notes

    home_page = getattr(settings, "homePage", None)
    if home_page is not None:
        page_template = getattr(home_page, "pageTemplate", None)
        if page_template:
            notes.append(f"Personalized homepage page template: {page_template}")
        dismissed = getattr(home_page, "dismissedAnnouncements", None) or []
        if dismissed:
            notes.append(f"{len(dismissed)} dismissed announcement(s) recorded")

    views_settings = getattr(settings, "views", None)
    default_view = getattr(views_settings, "defaultView", None) if views_settings else None
    if default_view:
        notes.append(f"Default view set: {default_view}")

    return notes
