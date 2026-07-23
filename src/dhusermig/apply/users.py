from __future__ import annotations

import logging
from typing import List, Optional

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.metadata.schema_classes import (
    CorpUserEditableInfoClass,
    CorpUserInfoClass,
    CorpUserSettingsClass,
    SystemMetadataClass,
)

logger = logging.getLogger(__name__)

CORPUSER_ASPECT_NAMES = [
    "corpUserInfo",
    "corpUserEditableInfo",
    "groupMembership",
    "nativeGroupMembership",
    "status",
    "roleMembership",
    "corpUserStatus",
    "corpUserSettings",
]


def create_new_user_from_old(
    graph: DataHubGraph,
    old_urn: str,
    new_urn: str,
    new_email: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """
    Fetch old user aspects and emit MCPs to create the new user with the same profile.
    If new_email is set, update email in corpUserInfo/corpUserEditableInfo to new_email.
    Group membership is copied as-is (groupMembership and nativeGroupMembership are
    distinct; do not overwrite with a merged list or the same group can appear in both).
    Returns the count of aspects emitted.
    """
    mcps: List[MetadataChangeProposalWrapper] = []
    old_mcps = graph.get_entity_as_mcps(old_urn, aspects=CORPUSER_ASPECT_NAMES)
    if not old_mcps:
        raise RuntimeError(
            f"No aspects fetched for {old_urn}; refusing to create an empty clone"
        )

    for mcp in old_mcps:
        aspect = mcp.aspect
        if new_email is not None:
            if isinstance(aspect, CorpUserInfoClass):
                obj = aspect.to_obj()
                obj["email"] = new_email
                obj["active"] = getattr(aspect, "active", True) or True
                aspect = CorpUserInfoClass.from_obj(obj)
            elif isinstance(aspect, CorpUserEditableInfoClass):
                obj = aspect.to_obj()
                obj["email"] = new_email
                aspect = CorpUserEditableInfoClass.from_obj(obj)
            elif isinstance(aspect, CorpUserSettingsClass):
                obj = aspect.to_obj()
                ns = obj.get("notificationSettings") or {}
                es = dict(ns.get("emailSettings") or {})
                es["email"] = new_email
                ns["emailSettings"] = es
                obj["notificationSettings"] = ns
                aspect = CorpUserSettingsClass.from_obj(obj)
        new_mcp = MetadataChangeProposalWrapper(
            entityUrn=new_urn,
            aspect=aspect,
            systemMetadata=SystemMetadataClass(runId="user-migration"),
        )
        mcps.append(new_mcp)
        if not dry_run:
            graph.emit_mcp(new_mcp)

    return len(mcps)
