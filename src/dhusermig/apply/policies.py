from __future__ import annotations

import copy
import logging
from typing import List, Optional

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.metadata.schema_classes import DataHubPolicyInfoClass, SystemMetadataClass

from dhusermig.graph import DiscoveryError, get_entity_via_api

logger = logging.getLogger(__name__)

# actors.resourceOwners on a policy is a bare boolean ("grant to whoever owns the
# resource"), not a per-user reference -- see DataHubActorFilterClass /
# ActorFilter.resourceOwners. It can't be matched to a specific user_urn by aspect
# inspection alone (that would require evaluating resource ownership per policy's
# resource filter, out of scope here). We only
# rewrite actors.users; policies_naming_user logs a count of resourceOwners=true
# policies it saw so an operator can review them separately.

LIST_POLICIES_QUERY = """
query listPolicies($input: ListPoliciesInput!) {
  listPolicies(input: $input) {
    policies {
      urn
      actors {
        users
        resourceOwners
      }
    }
  }
}
"""


def add_actor_to_policy_info(info: dict, new_urn: str) -> dict:
    out = copy.deepcopy(info)
    actors = out.setdefault("actors", {})
    users = actors.setdefault("users", [])
    if new_urn not in users:
        users.append(new_urn)
    return out


def remove_actor_from_policy_info(info: dict, old_urn: str) -> dict:
    out = copy.deepcopy(info)
    users = out.get("actors", {}).get("users", [])
    out["actors"]["users"] = [u for u in users if u != old_urn]
    return out


def policies_naming_user(
    graph: DataHubGraph,
    user_urn: str,
    page_size: int = 50,
) -> List[str]:
    """
    Return URNs of policies whose actors.users references user_urn.
    Paginated via GraphQL listPolicies. Does not match resourceOwners-based grants
    (see module notes above); logs a count of those instead. Raises DiscoveryError
    if the query errors -- the plan would silently miss policies otherwise.
    """
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        logger.warning("GraphQL unavailable; policy discovery skipped")
        return []
    urns: List[str] = []
    resource_owner_policy_count = 0
    start = 0
    try:
        while True:
            variables = {"input": {"start": start, "count": page_size}}
            result = execute(LIST_POLICIES_QUERY, variables=variables, operation_name="listPolicies")
            data = (result or {}).get("data") or result or {}
            list_data = data.get("listPolicies") if isinstance(data, dict) else None
            if not list_data or not isinstance(list_data, dict):
                break
            policies = list_data.get("policies") or []
            for p in policies:
                if not isinstance(p, dict):
                    continue
                actors = p.get("actors") or {}
                if user_urn in (actors.get("users") or []):
                    if p.get("urn"):
                        urns.append(p["urn"])
                if actors.get("resourceOwners"):
                    resource_owner_policy_count += 1
            if len(policies) < page_size:
                break
            start += page_size
    except Exception as e:
        raise DiscoveryError(f"Policy discovery failed for {user_urn}: {e}") from e
    if resource_owner_policy_count:
        logger.warning(
            "%d polic%s grant access via resourceOwners (owner-of-resource, not a named "
            "user); these cannot be matched to %s by aspect inspection and are not "
            "rewritten by this tool.",
            resource_owner_policy_count,
            "y" if resource_owner_policy_count == 1 else "ies",
            user_urn,
        )
    return urns


def apply_policy_rewrite(
    graph: DataHubGraph,
    gms_url: str,
    token: Optional[str],
    policy_urn: str,
    new_urn: str,
    remove_urn: Optional[str] = None,
    dry_run: bool = False,
) -> bool:
    """
    GET dataHubPolicyInfo via OpenAPI v3 generic (get_entity_via_api), add new_urn to
    actors.users and (if remove_urn is set) remove it, then write the aspect back via
    graph.emit_mcp -- the same MCP write path used by apply/users.py.
    """
    body = get_entity_via_api(gms_url, token, policy_urn)
    if not body:
        raise RuntimeError(f"Could not fetch policy {policy_urn}")
    info = body.get("dataHubPolicyInfo")
    if not info or not isinstance(info, dict):
        raise RuntimeError(f"No dataHubPolicyInfo in response for {policy_urn}")
    value = info.get("value") if isinstance(info.get("value"), dict) else info
    if not value:
        raise RuntimeError(f"dataHubPolicyInfo missing value for {policy_urn}")

    value = add_actor_to_policy_info(value, new_urn)
    if remove_urn:
        value = remove_actor_from_policy_info(value, remove_urn)

    if dry_run:
        logger.info("Dry run: would rewrite actors on policy %s", policy_urn)
        return True

    mcp = MetadataChangeProposalWrapper(
        entityUrn=policy_urn,
        aspect=DataHubPolicyInfoClass.from_obj(value),
        systemMetadata=SystemMetadataClass(runId="user-migration"),
    )
    graph.emit_mcp(mcp)
    return True
