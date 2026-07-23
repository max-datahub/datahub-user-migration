# tests/e2e/test_settle.py
import pytest

import tests.e2e.golden_utils as gu
from tests.e2e.golden_utils import wait_until


def test_wait_until_returns_when_predicate_becomes_true():
    calls = {"n": 0}

    def pred():
        calls["n"] += 1
        return calls["n"] >= 3  # true on 3rd poll

    wait_until(pred, timeout_s=100, interval_s=1, sleep=lambda s: None)
    assert calls["n"] == 3


def test_wait_until_times_out():
    # elapsed is driven by the injected sleep; predicate never true
    with pytest.raises(TimeoutError):
        wait_until(lambda: False, timeout_s=5, interval_s=2, sleep=lambda s: None)


def test_owned_entities_settled_superset(monkeypatch):
    # Bare object() as the graph: proves owned_entities_settled calls only the
    # (monkeypatched) free function, never a method on graph. With the old
    # method-call bug this would raise AttributeError instead of returning.
    monkeypatch.setattr(gu, "get_entity_urns_owned_by_user", lambda g, u: ["urn:a", "urn:b", "urn:c"])
    assert gu.owned_entities_settled(object(), "urn:li:corpuser:x", {"urn:a", "urn:b"}) is True

    monkeypatch.setattr(gu, "get_entity_urns_owned_by_user", lambda g, u: ["urn:a"])
    assert gu.owned_entities_settled(object(), "urn:li:corpuser:x", {"urn:a", "urn:b"}) is False
