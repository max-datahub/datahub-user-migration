"""Deterministic seed data for the e2e golden suite -- one MCP set covering
every OSS-supported migration case. build_seed_mcps()
is pure (no graph I/O) so it's unit-testable without a live GMS; seed_all()
emits it against a real DataHubGraph.
"""

from __future__ import annotations

from dataclasses import dataclass

from datahub.emitter.mce_builder import SYSTEM_ACTOR, make_dataset_urn, make_group_urn, make_user_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    AuditStampClass,
    CorpGroupInfoClass,
    CorpUserInfoClass,
    DataHubAccessTokenInfoClass,
    DataHubActorFilterClass,
    DataHubIngestionSourceConfigClass,
    DataHubIngestionSourceInfoClass,
    DataHubPolicyInfoClass,
    DataHubViewDefinitionClass,
    DataHubViewInfoClass,
    FilterClass,
    GroupMembershipClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    OwnershipTypeInfoClass,
    StatusClass,
)

OLD_EMAIL = "alice@example-source.tld"
NEW_EMAIL = "alice@example-target.tld"
TARGET_DOMAIN = "example-target.tld"
BOB_EMAIL = "bob@example-source.tld"

GROUP_URN = make_group_urn("analytics")

DS_MULTI_ENUM = make_dataset_urn("mysql", "my_db.my_schema.multi_enum", "PROD")
DS_MULTI_CUSTOM = make_dataset_urn("mysql", "my_db.my_schema.multi_custom", "PROD")
DS_OTHER = make_dataset_urn("mysql", "my_db.my_schema.other", "PROD")

OTYPE_STEWARD = "urn:li:ownershipType:steward"
OTYPE_DATA_STEWARD = "urn:li:ownershipType:data_steward"

POLICY_URN = "urn:li:dataHubPolicy:e2e-alice-policy"
TOKEN_URN = "urn:li:dataHubAccessToken:e2e-alice-token"
VIEW_URN = "urn:li:dataHubView:e2e-alice-view"
SOURCE_URN = "urn:li:dataHubIngestionSource:e2e-recreation-source"


@dataclass
class SeededRefs:
    old_email: str
    new_email: str
    target_domain: str
    bob_email: str
    group_urn: str
    ds_multi_enum: str
    ds_multi_custom: str
    ds_other: str
    otype_steward: str
    otype_data_steward: str
    policy_urn: str
    token_urn: str
    view_urn: str
    source_urn: str


def _refs() -> SeededRefs:
    return SeededRefs(
        old_email=OLD_EMAIL,
        new_email=NEW_EMAIL,
        target_domain=TARGET_DOMAIN,
        bob_email=BOB_EMAIL,
        group_urn=GROUP_URN,
        ds_multi_enum=DS_MULTI_ENUM,
        ds_multi_custom=DS_MULTI_CUSTOM,
        ds_other=DS_OTHER,
        otype_steward=OTYPE_STEWARD,
        otype_data_steward=OTYPE_DATA_STEWARD,
        policy_urn=POLICY_URN,
        token_urn=TOKEN_URN,
        view_urn=VIEW_URN,
        source_urn=SOURCE_URN,
    )


def build_seed_mcps() -> list[MetadataChangeProposalWrapper]:
    alice = make_user_urn(OLD_EMAIL)
    bob = make_user_urn(BOB_EMAIL)
    stamp = AuditStampClass(time=0, actor=SYSTEM_ACTOR)
    mcps = []

    def add(entity_urn: str, aspect) -> None:
        mcps.append(MetadataChangeProposalWrapper(entityUrn=entity_urn, aspect=aspect))

    add(alice, CorpUserInfoClass(active=True, displayName="Alice", email=OLD_EMAIL))
    add(bob, CorpUserInfoClass(active=True, displayName="Bob", email=BOB_EMAIL))
    # Establish a known-active status so a re-seed reverses any prior soft-delete
    # (cleanup soft-deletes the old user); makes the e2e re-runnable and the seed
    # state fully deterministic rather than inheriting a stale removed=true.
    add(alice, StatusClass(removed=False))
    add(bob, StatusClass(removed=False))

    add(GROUP_URN, CorpGroupInfoClass(admins=[], members=[alice], groups=[], displayName="Analytics"))
    add(alice, GroupMembershipClass(groups=[GROUP_URN]))

    add(OTYPE_STEWARD, OwnershipTypeInfoClass(name="Steward", created=stamp, lastModified=stamp))
    add(OTYPE_DATA_STEWARD, OwnershipTypeInfoClass(name="Data Steward", created=stamp, lastModified=stamp))

    add(
        DS_MULTI_ENUM,
        OwnershipClass(
            owners=[
                OwnerClass(owner=alice, type=OwnershipTypeClass.BUSINESS_OWNER),
                OwnerClass(owner=alice, type=OwnershipTypeClass.TECHNICAL_OWNER),
            ]
        ),
    )
    add(
        DS_MULTI_CUSTOM,
        OwnershipClass(
            owners=[
                OwnerClass(owner=alice, type=OwnershipTypeClass.CUSTOM, typeUrn=OTYPE_STEWARD),
                OwnerClass(owner=alice, type=OwnershipTypeClass.CUSTOM, typeUrn=OTYPE_DATA_STEWARD),
            ]
        ),
    )
    add(
        DS_OTHER,
        OwnershipClass(owners=[OwnerClass(owner=bob, type=OwnershipTypeClass.TECHNICAL_OWNER)]),
    )

    add(
        POLICY_URN,
        DataHubPolicyInfoClass(
            displayName="e2e alice policy",
            description="Policy naming alice as an actor (e2e seed)",
            type="PLATFORM",
            state="ACTIVE",
            privileges=["MANAGE_USERS"],
            actors=DataHubActorFilterClass(users=[alice], resourceOwners=False),
        ),
    )

    add(
        TOKEN_URN,
        DataHubAccessTokenInfoClass(
            name="e2e-alice-token",
            actorUrn=alice,
            ownerUrn=alice,
            createdAt=0,
        ),
    )

    add(
        VIEW_URN,
        DataHubViewInfoClass(
            name="Alice's personal view",
            type="PERSONAL",
            definition=DataHubViewDefinitionClass(entityTypes=["dataset"], filter=FilterClass()),
            created=AuditStampClass(time=0, actor=alice),
            lastModified=AuditStampClass(time=0, actor=alice),
        ),
    )

    add(
        SOURCE_URN,
        DataHubIngestionSourceInfoClass(
            name="e2e-recreation-source",
            type="snowflake",
            config=DataHubIngestionSourceConfigClass(
                recipe='{"source": {"type": "snowflake", "config": {"usage": {"enabled": true}}}}'
            ),
        ),
    )

    return mcps


def seed_all(graph) -> SeededRefs:
    for mcp in build_seed_mcps():
        graph.emit_mcp(mcp)
    return _refs()
