from __future__ import annotations

import logging
from typing import List, Optional

from datahub.ingestion.graph.client import DataHubGraph

from dhusermig.graph import get_entity_via_api, get_subscription_urns_for_user

logger = logging.getLogger(__name__)

CREATE_SUBSCRIPTION_MUTATION = """
mutation createSubscription($input: CreateSubscriptionInput!) {
  createSubscription(input: $input) {
    subscriptionUrn
    urn
    actorUrn
    entity { urn }
  }
}
"""

DELETE_SUBSCRIPTION_MUTATION = """
mutation deleteSubscription($input: DeleteSubscriptionInput!) {
  deleteSubscription(input: $input)
}
"""


def _get_subscription_details_via_api(
    gms_url: str,
    token: Optional[str],
    sub_urn: str,
) -> Optional[dict]:
    """
    GET subscription entity's subscriptionInfo aspect via OpenAPI v3 entity generic.
    Returns subscriptionInfo.value dict, or None if the request fails or the aspect is missing.
    """
    body = get_entity_via_api(gms_url, token, sub_urn)
    if not body:
        return None
    info = body.get("subscriptionInfo")
    if not info or not isinstance(info, dict):
        logger.debug("No subscriptionInfo in response for %s", sub_urn)
        return None
    value = info.get("value") if isinstance(info.get("value"), dict) else info
    if not value or not value.get("entityUrn"):
        logger.debug("subscriptionInfo missing entityUrn for %s", sub_urn)
        return None
    return value


def subscription_info_to_create_input(info_value: dict, new_user_urn: str) -> dict:
    """
    Map subscriptionInfo.value from GET entity generic to GraphQL CreateSubscriptionInput.
    """
    entity_urn = info_value.get("entityUrn") or ""
    types = info_value.get("types") or ["ENTITY_CHANGE"]
    entity_change_types = info_value.get("entityChangeTypes") or []
    notification_config = info_value.get("notificationConfig") or {}

    entity_change_inputs = []
    for ect in entity_change_types:
        if not isinstance(ect, dict):
            continue
        change_type = ect.get("entityChangeType")
        if not change_type:
            continue
        item = {"entityChangeType": change_type}
        if ect.get("filter") and isinstance(ect["filter"], dict):
            item["filter"] = {"includeAssertions": ect["filter"].get("includeAssertions")}
        entity_change_inputs.append(item)
    if not entity_change_inputs:
        entity_change_inputs = [{"entityChangeType": "OWNER_ADDED"}]

    notif = (
        notification_config.get("notificationSettings")
        if isinstance(notification_config, dict)
        else {}
    )
    notification_input = {}
    if isinstance(notif, dict):
        notification_input = {
            "sinkTypes": notif.get("sinkTypes"),
            "slackSettings": notif.get("slackSettings"),
            "emailSettings": notif.get("emailSettings"),
            "teamsSettings": notif.get("teamsSettings"),
            "settings": notif.get("settings"),
        }

    return {
        "entityUrn": entity_urn,
        "subscriptionTypes": types,
        "entityChangeTypes": entity_change_inputs,
        "notificationConfig": {"notificationSettings": notification_input},
        "userUrn": new_user_urn,
    }


def new_user_already_subscribed(existing_inputs: List[dict], candidate: dict) -> bool:
    """
    True if an existing subscription targets the same entityUrn with the same
    subscriptionTypes set. The idempotency fix: re-running migration must not
    double-create subscriptions.
    """
    key = (candidate.get("entityUrn"), frozenset(candidate.get("subscriptionTypes") or []))
    for e in existing_inputs:
        if (e.get("entityUrn"), frozenset(e.get("subscriptionTypes") or [])) == key:
            return True
    return False


def _create_subscription_via_graphql(
    graph: DataHubGraph,
    input_dict: dict,
    dry_run: bool,
) -> bool:
    """
    Call GraphQL createSubscription mutation. Returns True if created; raises on
    failure (transport/GraphQL errors propagate, a response without a subscription
    urn raises RuntimeError).
    """
    execute = getattr(graph, "execute_graphql", None)
    if not execute:
        raise RuntimeError("createSubscription requires DataHub Cloud GraphQL")
    if dry_run:
        logger.info("Dry run: would createSubscription for entity %s", input_dict.get("entityUrn"))
        return True
    result = execute(
        CREATE_SUBSCRIPTION_MUTATION,
        variables={"input": input_dict},
        operation_name="createSubscription",
    )
    data = (result or {}).get("data") or result or {}
    created = (data.get("createSubscription") or {}) if isinstance(data, dict) else {}
    if not (created.get("subscriptionUrn") or created.get("urn")):
        raise RuntimeError(
            f"createSubscription returned no subscriptionUrn for entity "
            f"{input_dict.get('entityUrn')}: {result}"
        )
    return True


def get_existing_subscription_inputs(
    graph: DataHubGraph,
    gms_url: str,
    token: Optional[str],
    user_urn: str,
    batch_size: int = 50,
) -> List[dict]:
    """
    Fetch user_urn's current subscriptions, shaped as CreateSubscriptionInput dicts, for
    use as the dedupe set passed to copy_subscription. Call once per new user (not once
    per copied subscription) and reuse the result across the loop.
    """
    existing: List[dict] = []
    for sub_urn in get_subscription_urns_for_user(graph, user_urn, batch_size=batch_size):
        info_value = _get_subscription_details_via_api(gms_url, token, sub_urn)
        if info_value:
            existing.append(subscription_info_to_create_input(info_value, user_urn))
    return existing


def copy_subscription(
    graph: DataHubGraph,
    gms_url: str,
    token: Optional[str],
    sub_urn: str,
    new_urn: str,
    existing_new_sub_entities: List[dict],
    dry_run: bool = False,
) -> bool:
    """
    Copy one old-user subscription (sub_urn) to new_urn: GET its details, build the
    CreateSubscriptionInput, skip if new_urn already holds an equivalent subscription
    (existing_new_sub_entities — new_urn's own current subscriptions, fetched once per
    user via get_existing_subscription_inputs), else createSubscription.

    Returns False ONLY for the benign dedupe skip; real failures raise.
    """
    info_value = _get_subscription_details_via_api(gms_url, token, sub_urn)
    if not info_value:
        raise RuntimeError(f"Could not fetch subscription details for {sub_urn}")
    input_dict = subscription_info_to_create_input(info_value, new_urn)
    if new_user_already_subscribed(existing_new_sub_entities, input_dict):
        logger.debug(
            "Skipping subscription %s: %s already subscribed to %s",
            sub_urn,
            new_urn,
            input_dict["entityUrn"],
        )
        return False
    return _create_subscription_via_graphql(graph, input_dict, dry_run)


def delete_subscription(
    graph: DataHubGraph,
    subscription_urn: str,
    dry_run: bool = False,
    hard: bool = False,
) -> bool:
    """
    Delete subscription via GraphQL deleteSubscription or entity delete. A GraphQL
    failure logs a warning and falls back to entity delete; a failure of the
    entity delete itself raises.
    """
    execute = getattr(graph, "execute_graphql", None)
    if execute and not dry_run:
        try:
            variables = {"input": {"subscriptionUrn": subscription_urn}}
            result = execute(
                DELETE_SUBSCRIPTION_MUTATION,
                variables=variables,
                operation_name="deleteSubscription",
            )
            data = ((result or {}).get("data") or {}) if isinstance(result, dict) else {}
            if data.get("deleteSubscription") is True:
                logger.info("Deleted subscription %s", subscription_urn)
                return True
        except Exception as e:
            logger.warning(
                "deleteSubscription GraphQL failed for %s (%s); falling back to entity delete",
                subscription_urn, e,
            )
    if dry_run:
        logger.info("[Dry run] Would delete subscription %s", subscription_urn)
        return True
    if hard:
        graph.hard_delete_entity(subscription_urn)
    else:
        graph.soft_delete_entity(subscription_urn)
    logger.info("%s-deleted subscription entity %s", "Hard" if hard else "Soft", subscription_urn)
    return True
