from __future__ import annotations

import logging
from typing import List

from datahub.ingestion.graph.client import DataHubGraph

from dhusermig.graph import DiscoveryError

logger = logging.getLogger(__name__)

# GraphQL listAccessTokens (auth.graphql). Filtering is via FacetFilterInput on
# the ACCESS_TOKEN search index -- confirmed field name is "ownerUrn", not
# "actorUrn" (see ListAccessTokensResolver.isListingSelfTokens in
# datahub-graphql-core, which matches filter.field == "ownerUrn" against the
# requesting actor).
LIST_ACCESS_TOKENS_QUERY = """
query listAccessTokens($input: ListAccessTokenInput!) {
  listAccessTokens(input: $input) {
    tokens {
      urn
    }
  }
}
"""


def active_tokens_for_user(
    graph: DataHubGraph,
    user_urn: str,
    page_size: int = 50,
) -> List[str]:
    """
    Return URNs of access tokens owned by user_urn. Detect-only: personal access
    tokens can't be recreated for a different user programmatically, so this is
    reported to the operator rather than acted on. Raises DiscoveryError if the
    query errors -- the plan would silently miss tokens otherwise.
    """
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        logger.warning("GraphQL unavailable; token discovery skipped")
        return []
    urns: List[str] = []
    start = 0
    try:
        while True:
            variables = {
                "input": {
                    "start": start,
                    "count": page_size,
                    "filters": [{"field": "ownerUrn", "condition": "EQUAL", "values": [user_urn]}],
                }
            }
            result = execute(LIST_ACCESS_TOKENS_QUERY, variables=variables, operation_name="listAccessTokens")
            data = (result or {}).get("data") or result or {}
            list_data = data.get("listAccessTokens") if isinstance(data, dict) else None
            if not list_data or not isinstance(list_data, dict):
                break
            tokens = list_data.get("tokens") or []
            for t in tokens:
                if isinstance(t, dict) and t.get("urn"):
                    urns.append(str(t["urn"]))
            if len(tokens) < page_size:
                break
            start += page_size
    except Exception as e:
        raise DiscoveryError(f"Token discovery failed for {user_urn}: {e}") from e
    return urns
