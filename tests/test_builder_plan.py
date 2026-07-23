# tests/test_builder_plan.py
import pytest
from datahub.metadata.schema_classes import OwnerClass, OwnershipClass

from dhusermig.apply import views as views_mod
from dhusermig.config import RunConfig
from dhusermig.plan import builder
from dhusermig.plan.builder import build_plan_from_references
from dhusermig.plan.schema import ChangeKind, State

OLD = "urn:li:corpuser:a@src.tld"
NEW = "urn:li:corpuser:a@dst.tld"


def test_build_migrate_changes_from_references():
    refs = {
        "ownership": [("urn:li:dataset:x", [("BUSINESS_OWNER", None), ("TECHNICAL_OWNER", None)])],
        "subscriptions": ["urn:li:subscription:s1"],
        "policies": ["urn:li:dataHubPolicy:p1"],
        "tokens": ["urn:li:accessToken:t1"],
        "views": ["urn:li:dataHubView:v1"],
        "homepage": ["default view customized"],
    }
    changes = build_plan_from_references(refs, phase="migrate", new_urn=NEW, old_urn=OLD)
    kinds = [c.kind for c in changes]
    assert ChangeKind.CREATE_USER in kinds
    add = next(c for c in changes if c.kind == ChangeKind.ADD_OWNERSHIP)
    assert add.owner_types == [["BUSINESS_OWNER", None], ["TECHNICAL_OWNER", None]]
    assert any(c.kind == ChangeKind.DETECT_TOKEN and c.state == State.INFO for c in changes)
    assert any(c.kind == ChangeKind.DETECT_HOMEPAGE and c.state == State.INFO for c in changes)


def test_changes_are_deterministically_ordered_regardless_of_discovery_order():
    # Discovery uses sets, so ownership refs can arrive in any order; the
    # emitted change list must be identical either way (deterministic plan).
    o1 = ("urn:li:dataset:aaa", [("TECHNICAL_OWNER", None)])
    o2 = ("urn:li:dataset:bbb", [("BUSINESS_OWNER", None)])
    forward = build_plan_from_references({"ownership": [o1, o2]}, phase="migrate", new_urn=NEW, old_urn=OLD)
    reverse = build_plan_from_references({"ownership": [o2, o1]}, phase="migrate", new_urn=NEW, old_urn=OLD)

    def _key(c):
        return (c.kind.value, c.target, str(c.owner_types))

    assert [_key(c) for c in forward] == [_key(c) for c in reverse]


def test_migrate_personal_views_are_info_not_pending():
    refs = {"views": ["urn:li:dataHubView:v1"]}
    changes = build_plan_from_references(refs, phase="migrate", new_urn=NEW, old_urn=OLD)
    view_change = next(c for c in changes if c.kind == ChangeKind.MIGRATE_VIEW)
    assert view_change.state == State.INFO


def test_migrate_recreation_sources_are_info():
    refs = {"recreation_sources": ["urn:li:dataHubIngestionSource:src1"]}
    changes = build_plan_from_references(refs, phase="migrate", new_urn=NEW, old_urn=OLD)
    finding = next(c for c in changes if c.kind == ChangeKind.DETECT_RECREATION_SOURCE)
    assert finding.state == State.INFO
    assert finding.target == "urn:li:dataHubIngestionSource:src1"


def test_build_cleanup_changes_from_references():
    refs = {
        "ownership": [("urn:li:dataset:x", [])],
        "subscriptions": ["urn:li:subscription:s1"],
        "policies": ["urn:li:dataHubPolicy:p1"],
    }
    changes = build_plan_from_references(refs, phase="cleanup", new_urn=NEW, old_urn=OLD)
    kinds = {c.kind for c in changes}
    assert kinds == {
        ChangeKind.REMOVE_OWNERSHIP,
        ChangeKind.DELETE_SUBSCRIPTION,
        ChangeKind.REMOVE_POLICY_ACTOR,
        ChangeKind.DELETE_USER,
        ChangeKind.REINDEX_USER,
    }
    delete_user = next(c for c in changes if c.kind == ChangeKind.DELETE_USER)
    assert delete_user.target == OLD
    assert all(c.state == State.PENDING for c in changes)


class FakeGraph:
    """Fake DataHubGraph exposing only get_ownership, matching the SDK method
    build_plan calls per entity to capture every ownership type the old user holds."""

    def get_ownership(self, entity_urn):
        return OwnershipClass(
            owners=[
                OwnerClass(owner=OLD, type="BUSINESS_OWNER"),
                OwnerClass(owner=OLD, type="TECHNICAL_OWNER"),
                OwnerClass(owner="urn:li:corpuser:other@x.tld", type="TECHNICAL_OWNER"),
            ]
        )


def _patch_collectors(monkeypatch, *, user_exists=True):
    monkeypatch.setattr(builder, "get_graph", lambda cfg: FakeGraph())
    monkeypatch.setattr(builder, "user_exists_via_api", lambda gms_url, token, urn: user_exists)
    monkeypatch.setattr(builder, "get_entity_urns_owned_by_user", lambda graph, urn: ["urn:li:dataset:x"])
    monkeypatch.setattr(builder, "get_subscription_urns_for_user", lambda graph, urn: [])
    monkeypatch.setattr(builder, "policies_naming_user", lambda graph, urn: [])
    monkeypatch.setattr(builder, "active_tokens_for_user", lambda graph, urn: [])
    monkeypatch.setattr(builder, "recreation_sources", lambda graph: [])
    monkeypatch.setattr(views_mod, "personal_views_for_user", lambda graph, urn: [])
    monkeypatch.setattr(views_mod, "homepage_findings_for_user", lambda graph, urn: [])


OLD_TRIPLE = ("a@src.tld", "a@dst.tld", "urn:li:corpuser:a@src.tld")


def test_build_plan_captures_all_owner_types_per_entity(monkeypatch):
    _patch_collectors(monkeypatch)
    cfg = RunConfig(gms_url="http://gms", token=None)

    plan = builder.build_plan(cfg, [OLD_TRIPLE], phase="migrate", options={})

    add = next(c for c in plan.users[0].changes if c.kind == ChangeKind.ADD_OWNERSHIP)
    assert add.owner_types == [["BUSINESS_OWNER", None], ["TECHNICAL_OWNER", None]]
    assert plan.meta.phase == "migrate"
    assert plan.meta.target_domain == "dst.tld"


def test_build_plan_aborts_if_old_user_missing(monkeypatch):
    _patch_collectors(monkeypatch, user_exists=False)
    cfg = RunConfig(gms_url="http://gms", token=None)

    with pytest.raises(ValueError):
        builder.build_plan(cfg, [OLD_TRIPLE], phase="migrate", options={})


def test_resolve_pairs_source_domain_threads_urn_and_skips_username_urns(monkeypatch):
    discovered = [
        {"urn": "urn:li:corpuser:alice@src.tld", "email": "alice@src.tld"},  # email URN -> migrate
        {"urn": "urn:li:corpuser:Bob@Src.tld", "email": "bob@src.tld"},      # non-normalized email URN -> thread real URN
        {"urn": "urn:li:corpuser:svc.account", "email": "svc.account@src.tld"},  # username URN -> skip
    ]
    monkeypatch.setattr(
        builder, "discover_users",
        lambda gms_url, token, domain_filter=None: discovered,
    )
    cfg = RunConfig(gms_url="http://gms", token=None)

    triples = builder.resolve_pairs(cfg, source_domain="src.tld", target_domain="dst.tld")

    assert triples == [
        ("alice@src.tld", "alice@dst.tld", "urn:li:corpuser:alice@src.tld"),
        ("bob@src.tld", "bob@dst.tld", "urn:li:corpuser:Bob@Src.tld"),  # real URN threaded, not reconstructed
    ]


class CustomTypesGraph:
    """get_ownership returns two CUSTOM owners for OLD with distinct typeUrns --
    regression guard for the dedup-on-type-alone bug (would collapse both down
    to a single CUSTOM entry and drop a typeUrn)."""

    def get_ownership(self, entity_urn):
        return OwnershipClass(
            owners=[
                OwnerClass(owner=OLD, type="CUSTOM", typeUrn="urn:li:ownershipType:steward"),
                OwnerClass(owner=OLD, type="CUSTOM", typeUrn="urn:li:ownershipType:approver"),
            ]
        )


def test_build_plan_preserves_distinct_custom_typeurns(monkeypatch):
    _patch_collectors(monkeypatch)
    monkeypatch.setattr(builder, "get_graph", lambda cfg: CustomTypesGraph())
    cfg = RunConfig(gms_url="http://gms", token=None)

    plan = builder.build_plan(cfg, [OLD_TRIPLE], phase="migrate", options={})

    add = next(c for c in plan.users[0].changes if c.kind == ChangeKind.ADD_OWNERSHIP)
    # Discriminating: type-only dedup would produce a single ["CUSTOM", ...] pair here.
    assert add.owner_types == [
        ["CUSTOM", "urn:li:ownershipType:steward"],
        ["CUSTOM", "urn:li:ownershipType:approver"],
    ]

    from dhusermig.apply.ownership import build_ownership_with_owner

    own = CustomTypesGraph().get_ownership("urn:li:dataset:x")
    new_own = build_ownership_with_owner(own, NEW, add.owner_types)
    new_custom_typeurns = {o.typeUrn for o in new_own.owners if o.owner == NEW}
    assert new_custom_typeurns == {"urn:li:ownershipType:steward", "urn:li:ownershipType:approver"}


class FlakyOwnershipGraph:
    """get_ownership raises persistently for one entity -- the plan build must
    abort loudly (DiscoveryError) rather than silently omit that entity's
    ownership from the plan."""

    BAD_URN = "urn:li:dataset:bad"
    GOOD_URN = "urn:li:dataset:good"

    def get_ownership(self, entity_urn):
        if entity_urn == self.BAD_URN:
            raise ConnectionError("simulated persistent network error")
        return OwnershipClass(owners=[OwnerClass(owner=OLD, type="TECHNICAL_OWNER")])


def test_build_plan_aborts_on_persistent_get_ownership_error(monkeypatch):
    _patch_collectors(monkeypatch)
    monkeypatch.setattr(builder, "get_graph", lambda cfg: FlakyOwnershipGraph())
    monkeypatch.setattr(
        builder,
        "get_entity_urns_owned_by_user",
        lambda graph, urn: [FlakyOwnershipGraph.BAD_URN, FlakyOwnershipGraph.GOOD_URN],
    )
    monkeypatch.setattr(builder.time, "sleep", lambda seconds: None)
    cfg = RunConfig(gms_url="http://gms", token=None)

    with pytest.raises(builder.DiscoveryError, match=FlakyOwnershipGraph.BAD_URN):
        builder.build_plan(cfg, [OLD_TRIPLE], phase="migrate", options={})
