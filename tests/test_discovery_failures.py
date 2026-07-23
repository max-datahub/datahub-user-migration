# tests/test_discovery_failures.py
"""Loud-fail plan collectors: a discovery error must abort plan building
(DiscoveryError) instead of silently producing a partial plan."""
from types import SimpleNamespace

import pytest

from dhusermig import graph as graph_mod
from dhusermig.apply.policies import policies_naming_user
from dhusermig.apply.prevention import recreation_sources
from dhusermig.apply.tokens import active_tokens_for_user
from dhusermig.apply.views import homepage_findings_for_user, personal_views_for_user
from dhusermig.graph import (
    DiscoveryError,
    get_entity_urns_owned_by_user,
    get_subscription_urns_for_user,
)
from dhusermig.plan import builder

USER = "urn:li:corpuser:a@src.tld"


class RaisingGraph:
    """Every collector entry point raises."""

    def get_related_entities(self, **kwargs):
        raise ConnectionError("boom")

    def get_urns_by_filter(self, **kwargs):
        raise ConnectionError("boom")

    def execute_graphql(self, *args, **kwargs):
        raise ConnectionError("boom")

    def get_aspect(self, *args, **kwargs):
        raise ConnectionError("boom")

    def list_all_entity_urns(self, *args, **kwargs):
        raise ConnectionError("boom")


# --- ownership discovery -----------------------------------------------------


def test_ownership_discovery_raises_when_both_paths_fail():
    with pytest.raises(DiscoveryError, match=USER):
        get_entity_urns_owned_by_user(RaisingGraph(), USER)


def test_ownership_discovery_raises_when_relationships_fail_and_search_unavailable():
    class G:
        def get_related_entities(self, **kwargs):
            raise ConnectionError("boom")

        # no get_urns_by_filter at all

    with pytest.raises(DiscoveryError):
        get_entity_urns_owned_by_user(G(), USER)


def test_ownership_discovery_proceeds_when_relationships_ok_but_search_fails():
    class G:
        def get_related_entities(self, **kwargs):
            return [SimpleNamespace(urn="urn:li:dataset:x")]

        def get_urns_by_filter(self, **kwargs):
            raise ConnectionError("boom")

    assert get_entity_urns_owned_by_user(G(), USER) == ["urn:li:dataset:x"]


def test_ownership_discovery_proceeds_when_relationships_fail_but_search_ok():
    class G:
        def get_related_entities(self, **kwargs):
            raise ConnectionError("boom")

        def get_urns_by_filter(self, **kwargs):
            return iter(["urn:li:dataset:x"])

    assert get_entity_urns_owned_by_user(G(), USER) == ["urn:li:dataset:x"]


# --- per-collector loud failure ----------------------------------------------


def test_policies_naming_user_raises_on_graphql_error():
    with pytest.raises(DiscoveryError, match=USER):
        policies_naming_user(RaisingGraph(), USER)


def test_active_tokens_for_user_raises_on_graphql_error():
    with pytest.raises(DiscoveryError, match=USER):
        active_tokens_for_user(RaisingGraph(), USER)


def test_personal_views_for_user_raises_on_graphql_error():
    with pytest.raises(DiscoveryError, match=USER):
        personal_views_for_user(RaisingGraph(), USER)


def test_homepage_findings_for_user_raises_on_aspect_error():
    with pytest.raises(DiscoveryError, match=USER):
        homepage_findings_for_user(RaisingGraph(), USER)


def test_recreation_sources_raises_on_graphql_error():
    with pytest.raises(DiscoveryError):
        recreation_sources(RaisingGraph())


def test_collectors_return_empty_when_graphql_unavailable():
    g = object()  # no execute_graphql
    assert policies_naming_user(g, USER) == []
    assert active_tokens_for_user(g, USER) == []
    assert personal_views_for_user(g, USER) == []
    assert recreation_sources(g) == []


# --- subscriptions -----------------------------------------------------------


def _patch_subscription_info_class(monkeypatch):
    # The installed OSS SDK may not ship SubscriptionInfoClass (Cloud-only);
    # inject a stand-in so the entity-listing fallback path runs.
    monkeypatch.setattr(
        "datahub.metadata.schema_classes.SubscriptionInfoClass", object, raising=False
    )


def test_subscriptions_graphql_failure_falls_back_to_entity_listing(monkeypatch):
    _patch_subscription_info_class(monkeypatch)

    class G(RaisingGraph):
        def list_all_entity_urns(self, entity, start, count):
            return ["urn:li:subscription:s1"] if start == 0 else []

        def get_aspect(self, urn, aspect_cls):
            return SimpleNamespace(actorUrn=USER)

    assert get_subscription_urns_for_user(G(), USER) == ["urn:li:subscription:s1"]


def test_subscriptions_raise_when_fallback_also_fails(monkeypatch):
    _patch_subscription_info_class(monkeypatch)
    with pytest.raises(DiscoveryError, match=USER):
        get_subscription_urns_for_user(RaisingGraph(), USER)


# --- builder: ownership retry + discover_users --------------------------------


def test_get_ownership_with_retry_raises_after_retries(monkeypatch):
    monkeypatch.setattr(builder.time, "sleep", lambda s: None)
    calls = []

    class G:
        def get_ownership(self, urn):
            calls.append(urn)
            raise ConnectionError("boom")

    with pytest.raises(DiscoveryError, match="urn:li:dataset:x"):
        builder._get_ownership_with_retry(G(), "urn:li:dataset:x")
    assert len(calls) == builder.MAX_GET_OWNERSHIP_RETRIES


def test_discover_users_raises_on_aspect_fetch_error(monkeypatch):
    class G:
        def list_all_entity_urns(self, entity, start, count):
            return [USER] if start == 0 else []

        def get_aspect(self, urn, aspect_cls):
            raise ConnectionError("boom")

    monkeypatch.setattr(builder, "_get_graph", lambda gms_url, token=None: G())
    with pytest.raises(DiscoveryError, match=USER):
        builder.discover_users("http://gms")


def test_discover_users_treats_missing_aspect_as_no_email(monkeypatch):
    class G:
        def list_all_entity_urns(self, entity, start, count):
            return [USER] if start == 0 else []

        def get_aspect(self, urn, aspect_cls):
            return None

    monkeypatch.setattr(builder, "_get_graph", lambda gms_url, token=None: G())
    assert builder.discover_users("http://gms") == [{"urn": USER, "email": USER}]


def test_discovery_error_is_runtime_error():
    assert issubclass(graph_mod.DiscoveryError, RuntimeError)
