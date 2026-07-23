from __future__ import annotations

import json
import logging
import urllib.request
from typing import List, Optional

from datahub.ingestion.graph.client import DataHubGraph

from dhusermig.graph import DiscoveryError

logger = logging.getLogger(__name__)

# GraphQL listIngestionSources (ingestion.graphql); config.recipe is the
# JSON-encoded recipe string.
LIST_INGESTION_SOURCES_QUERY = """
query listIngestionSources($input: ListIngestionSourcesInput!) {
  listIngestionSources(input: $input) {
    ingestionSources {
      urn
      type
      name
      config {
        recipe
      }
    }
  }
}
"""

# Source types that extract query-history-based usage (and so can recreate
# corpuser entities for deleted/migrated users) if usage extraction is on.
_USAGE_CAPABLE_TYPES = ("snowflake", "bigquery")
_USAGE_RECIPE_MARKER = "usage"
_USAGE_REPORTING_MARKER = "usage-reporting"


def recreation_sources(graph: DataHubGraph, page_size: int = 50) -> List[str]:
    """
    Return URNs of ingestion sources whose type/recipe indicates they can
    recreate corpuser entities from query-history actors (Snowflake/BigQuery
    usage extraction, or a dedicated usage-reporting source). Detect-only --
    flags these for operator review rather than modifying them. Raises
    DiscoveryError if the query errors -- the plan would silently miss
    recreation risks otherwise.
    """
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        logger.warning("GraphQL unavailable; ingestion-source (recreation risk) discovery skipped")
        return []
    urns: List[str] = []
    start = 0
    try:
        while True:
            variables = {"input": {"start": start, "count": page_size}}
            result = execute(
                LIST_INGESTION_SOURCES_QUERY, variables=variables, operation_name="listIngestionSources"
            )
            data = (result or {}).get("data") or result or {}
            list_data = data.get("listIngestionSources") if isinstance(data, dict) else None
            if not list_data or not isinstance(list_data, dict):
                break
            sources = list_data.get("ingestionSources") or []
            for s in sources:
                if not isinstance(s, dict):
                    continue
                source_type = (s.get("type") or "").lower()
                name = (s.get("name") or "").lower()
                recipe = ((s.get("config") or {}).get("recipe") or "").lower()
                is_usage_reporting = _USAGE_REPORTING_MARKER in source_type or _USAGE_REPORTING_MARKER in name
                is_usage_capable = any(t in source_type for t in _USAGE_CAPABLE_TYPES) and (
                    _USAGE_RECIPE_MARKER in recipe
                )
                if (is_usage_reporting or is_usage_capable) and s.get("urn"):
                    urns.append(str(s["urn"]))
            if len(sources) < page_size:
                break
            start += page_size
    except Exception as e:
        raise DiscoveryError(f"Ingestion-source (recreation risk) discovery failed: {e}") from e
    return urns


def recipe_fix_snippet() -> str:
    """
    Recipe guidance for preventing usage-based ingestion from recreating
    deleted/migrated users (datahub-project/datahub issue #7524). Field names are
    illustrative -- map them to the specific connector's actual usage-config
    toggle (e.g. Snowflake/BigQuery `usage.include_usage_stats`).
    """
    return (
        "To stop a usage-based ingestion source from recreating deleted/migrated "
        "users, either:\n"
        "  1. Disable usage extraction entirely: set `user_usage_enabled: false` "
        "in the source's usage config block, OR\n"
        "  2. Scope usage extraction to known-good users: set a `user_email_pattern` "
        "allow-list (e.g. allow: ['^.*@newdomain\\.com$']) and/or "
        "`pushdown_allow_usernames` to the migrated usernames only."
    )


def reindex_user(
    gms_url: str,
    token: Optional[str],
    user_urn: str,
    dry_run: bool = False,
) -> bool:
    """
    Reindex a single corpuser entity via the Operations restoreIndices endpoint
    (POST {gms}/operations?action=restoreIndices, payload {"urn": user_urn}) so a
    hard-deleted-then-recreated user's search-index state (e.g. a stale
    "Inactive" flag) is refreshed from primary storage. Endpoint path/payload
    confirmed against DataHubGraph.restore_indices in the installed acryl-datahub
    SDK (datahub/ingestion/graph/client.py). Raises RuntimeError on HTTP failure.
    """
    if dry_run:
        logger.info("Dry run: would reindex %s via operations?action=restoreIndices", user_urn)
        return True
    base = gms_url.rstrip("/")
    url = f"{base}/operations?action=restoreIndices"
    payload = json.dumps({"urn": user_urn}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except Exception as e:
        raise RuntimeError(f"Reindex failed for {user_urn}: {e}") from e
