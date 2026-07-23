# tests/test_verify.py
from datahub.metadata.schema_classes import OwnerClass, OwnershipClass

from dhusermig.verify import compare_counts, missing_expected_ownership, remaining_old_ownership


def test_counts_match():
    before = {"ownership": {"urn:li:dataset:x"}, "subscription": 2, "group_membership": 1}
    after = {"ownership": {"urn:li:dataset:x"}, "subscription": 2, "group_membership": 1}
    assert compare_counts(before, after) == []


def test_ownership_set_mismatch_reported():
    before = {"ownership": {"urn:li:dataset:x", "urn:li:dataset:y"}, "subscription": 0, "group_membership": 0}
    after = {"ownership": {"urn:li:dataset:x"}, "subscription": 0, "group_membership": 0}
    msgs = compare_counts(before, after)
    assert any("ownership" in m for m in msgs)


X = "urn:li:dataset:x"
Y = "urn:li:dataset:y"
NEW_URN = "urn:li:corpuser:a@dst.tld"
OLD_URN = "urn:li:corpuser:a@src.tld"
OTHER_URN = "urn:li:corpuser:someone-else@x.tld"


class FakeGraph:
    """Fake graph exposing only get_ownership -- verify's actual source of
    truth (the aspect from primary storage), not a search/relationship index."""

    def __init__(self, ownership_by_urn: dict):
        self._ownership_by_urn = ownership_by_urn

    def get_ownership(self, entity_urn):
        return self._ownership_by_urn.get(entity_urn)


def test_missing_expected_ownership_pass_when_new_owner_present():
    graph = FakeGraph({
        X: OwnershipClass(owners=[OwnerClass(owner=NEW_URN, type="TECHNICAL_OWNER")]),
        Y: OwnershipClass(owners=[OwnerClass(owner=NEW_URN, type="TECHNICAL_OWNER")]),
    })
    assert missing_expected_ownership(graph, NEW_URN, [X, Y]) == []


def test_missing_expected_ownership_fail_when_new_owner_absent():
    graph = FakeGraph({
        X: OwnershipClass(owners=[OwnerClass(owner=NEW_URN, type="TECHNICAL_OWNER")]),
        Y: OwnershipClass(owners=[OwnerClass(owner=OTHER_URN, type="TECHNICAL_OWNER")]),
    })
    # Discriminating: reads each target's Ownership aspect individually via
    # get_ownership, so it correctly tells X (owned) apart from Y (not owned).
    assert missing_expected_ownership(graph, NEW_URN, [X, Y]) == [Y]


def test_remaining_old_ownership_pass_when_old_owner_absent():
    graph = FakeGraph({X: OwnershipClass(owners=[OwnerClass(owner=NEW_URN, type="TECHNICAL_OWNER")])})
    assert remaining_old_ownership(graph, OLD_URN, [X]) == []


def test_remaining_old_ownership_fail_when_old_owner_still_present():
    graph = FakeGraph({X: OwnershipClass(owners=[OwnerClass(owner=OLD_URN, type="TECHNICAL_OWNER")])})
    assert remaining_old_ownership(graph, OLD_URN, [X]) == [X]
