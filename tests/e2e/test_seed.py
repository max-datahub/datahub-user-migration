from datahub.metadata.schema_classes import OwnershipClass

from tests.e2e import seed


def _mcps_by_entity():
    out = {}
    for mcp in seed.build_seed_mcps():
        out.setdefault(mcp.entityUrn, []).append(mcp.aspect)
    return out


def test_multi_custom_types_on_one_entity():
    """ds_multi_custom must have alice as TWO CUSTOM owners with DISTINCT typeUrns --
    the exact collapse case the tool's custom-typeUrn fix guards."""
    aspects = _mcps_by_entity()[seed.DS_MULTI_CUSTOM]
    ownership = next(a for a in aspects if isinstance(a, OwnershipClass))
    alice = seed.make_user_urn(seed.OLD_EMAIL)
    custom_pairs = {(o.owner, getattr(o, "typeUrn", None)) for o in ownership.owners if o.owner == alice}
    assert (alice, seed.OTYPE_STEWARD) in custom_pairs
    assert (alice, seed.OTYPE_DATA_STEWARD) in custom_pairs
    assert len(custom_pairs) == 2  # two distinct custom types, not collapsed


def test_multi_enum_types_on_one_entity():
    aspects = _mcps_by_entity()[seed.DS_MULTI_ENUM]
    ownership = next(a for a in aspects if isinstance(a, OwnershipClass))
    alice = seed.make_user_urn(seed.OLD_EMAIL)
    types = {o.type for o in ownership.owners if o.owner == alice}
    assert {"BUSINESS_OWNER", "TECHNICAL_OWNER"} <= types


def test_bob_owns_the_non_interference_dataset():
    aspects = _mcps_by_entity()[seed.DS_OTHER]
    ownership = next(a for a in aspects if isinstance(a, OwnershipClass))
    owners = {o.owner for o in ownership.owners}
    assert seed.make_user_urn(seed.BOB_EMAIL) in owners
    assert seed.make_user_urn(seed.OLD_EMAIL) not in owners


def test_all_detect_only_cases_present():
    entities = {mcp.entityUrn for mcp in seed.build_seed_mcps()}
    assert seed.POLICY_URN in entities  # policy naming alice
    assert seed.TOKEN_URN in entities  # PAT owned by alice
    assert seed.VIEW_URN in entities  # personal view created by alice
    assert seed.SOURCE_URN in entities  # recreation source
