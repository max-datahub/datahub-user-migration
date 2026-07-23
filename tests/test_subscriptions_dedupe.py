# tests/test_subscriptions_dedupe.py
import pytest

from dhusermig.apply import subscriptions
from dhusermig.apply.subscriptions import (
    copy_subscription,
    new_user_already_subscribed,
    subscription_info_to_create_input,
)


def test_create_input_maps_entity_and_actor():
    info = {"entityUrn": "urn:li:dataset:x", "types": ["ENTITY_CHANGE"],
            "entityChangeTypes": [{"entityChangeType": "OWNER_ADDED"}]}
    inp = subscription_info_to_create_input(info, "urn:li:corpuser:a@dst.tld")
    assert inp["entityUrn"] == "urn:li:dataset:x"
    assert inp["userUrn"] == "urn:li:corpuser:a@dst.tld"


def test_dedupe_same_entity_and_types():
    existing = [{"entityUrn": "urn:li:dataset:x", "subscriptionTypes": ["ENTITY_CHANGE"]}]
    cand = {"entityUrn": "urn:li:dataset:x", "subscriptionTypes": ["ENTITY_CHANGE"]}
    assert new_user_already_subscribed(existing, cand) is True


def test_dedupe_allows_different_entity():
    existing = [{"entityUrn": "urn:li:dataset:x", "subscriptionTypes": ["ENTITY_CHANGE"]}]
    cand = {"entityUrn": "urn:li:dataset:y", "subscriptionTypes": ["ENTITY_CHANGE"]}
    assert new_user_already_subscribed(existing, cand) is False


class RecordingGraph:
    """Fake graph whose execute_graphql records that it was called and returns a
    plausible success response — it must never raise, so a call is only ever
    detected via `.called`, not swallowed by copy_subscription's callee."""

    def __init__(self):
        self.called = False

    def execute_graphql(self, *args, **kwargs):
        self.called = True
        return {"data": {"createSubscription": {"subscriptionUrn": "urn:li:subscription:new"}}}


def test_copy_subscription_skips_when_new_user_already_subscribed(monkeypatch):
    # Wiring check for the idempotency fix: copy_subscription must not call
    # createSubscription when the new user already holds an equivalent subscription.
    # Uses a call-recording double, not a raised exception: the assertion on
    # graph.called runs after copy_subscription returns, outside any try/except,
    # so it can't be swallowed the way an exception raised inside execute_graphql
    # would be (_create_subscription_via_graphql catches Exception and returns False).
    monkeypatch.setattr(
        subscriptions,
        "_get_subscription_details_via_api",
        lambda gms_url, token, sub_urn: {
            "entityUrn": "urn:li:dataset:x",
            "types": ["ENTITY_CHANGE"],
        },
    )

    existing = [{"entityUrn": "urn:li:dataset:x", "subscriptionTypes": ["ENTITY_CHANGE"]}]
    graph = RecordingGraph()
    created = copy_subscription(
        graph, "http://gms", None, "urn:li:subscription:old", "urn:li:corpuser:a@dst.tld",
        existing,
    )
    assert graph.called is False
    assert created is False


def test_copy_subscription_creates_when_not_already_subscribed(monkeypatch):
    # Symmetry case: with no matching existing subscription, copy_subscription must
    # still attempt the create. This is what makes the skip-case assertion above
    # meaningful — if the dedupe check were deleted, this test would still pass,
    # but the skip-case test's `graph.called is False` would now fail.
    monkeypatch.setattr(
        subscriptions,
        "_get_subscription_details_via_api",
        lambda gms_url, token, sub_urn: {
            "entityUrn": "urn:li:dataset:x",
            "types": ["ENTITY_CHANGE"],
        },
    )

    graph = RecordingGraph()
    created = copy_subscription(
        graph, "http://gms", None, "urn:li:subscription:old", "urn:li:corpuser:a@dst.tld",
        [],
    )
    assert graph.called is True
    assert created is True


_INFO = {"entityUrn": "urn:li:dataset:x", "types": ["ENTITY_CHANGE"]}


def test_copy_subscription_raises_when_details_unfetchable(monkeypatch):
    monkeypatch.setattr(subscriptions, "_get_subscription_details_via_api", lambda *a: None)
    with pytest.raises(RuntimeError, match="urn:li:subscription:old"):
        copy_subscription(RecordingGraph(), "http://gms", None, "urn:li:subscription:old",
                          "urn:li:corpuser:a@dst.tld", [])


def test_copy_subscription_raises_without_graphql(monkeypatch):
    monkeypatch.setattr(subscriptions, "_get_subscription_details_via_api", lambda *a: _INFO)

    class NoGraphqlGraph:
        pass

    with pytest.raises(RuntimeError, match="DataHub Cloud"):
        copy_subscription(NoGraphqlGraph(), "http://gms", None, "urn:li:subscription:old",
                          "urn:li:corpuser:a@dst.tld", [])


def test_copy_subscription_raises_when_mutation_returns_no_urn(monkeypatch):
    monkeypatch.setattr(subscriptions, "_get_subscription_details_via_api", lambda *a: _INFO)

    class EmptyResponseGraph:
        def execute_graphql(self, *a, **k):
            return {"data": {"createSubscription": {}}}

    with pytest.raises(RuntimeError, match="urn:li:dataset:x"):
        copy_subscription(EmptyResponseGraph(), "http://gms", None, "urn:li:subscription:old",
                          "urn:li:corpuser:a@dst.tld", [])


def test_copy_subscription_propagates_transport_errors(monkeypatch):
    monkeypatch.setattr(subscriptions, "_get_subscription_details_via_api", lambda *a: _INFO)

    class RaisingGraph:
        def execute_graphql(self, *a, **k):
            raise ConnectionError("gms down")

    with pytest.raises(ConnectionError):
        copy_subscription(RaisingGraph(), "http://gms", None, "urn:li:subscription:old",
                          "urn:li:corpuser:a@dst.tld", [])


def test_delete_subscription_propagates_entity_delete_failure():
    class FailingDeleteGraph:  # no execute_graphql -> straight to entity delete
        def soft_delete_entity(self, urn):
            raise RuntimeError("delete rejected")

    with pytest.raises(RuntimeError, match="delete rejected"):
        subscriptions.delete_subscription(FailingDeleteGraph(), "urn:li:subscription:s")
