import copy

import pytest

from dhusermig.apply import policies
from dhusermig.apply.policies import (
    add_actor_to_policy_info,
    apply_policy_rewrite,
    remove_actor_from_policy_info,
)

OLD = "urn:li:corpuser:a@src.tld"
NEW = "urn:li:corpuser:a@dst.tld"
POLICY_URN = "urn:li:dataHubPolicy:1"


def test_add_actor_when_absent():
    info = {"actors": {"users": [OLD], "allUsers": False}}
    original = copy.deepcopy(info)
    out = add_actor_to_policy_info(info, NEW)
    assert NEW in out["actors"]["users"] and OLD in out["actors"]["users"]
    assert info == original


def test_add_actor_is_idempotent():
    info = {"actors": {"users": [OLD, NEW]}}
    original = copy.deepcopy(info)
    out = add_actor_to_policy_info(info, NEW)
    assert out["actors"]["users"].count(NEW) == 1
    assert info == original


def test_remove_actor():
    info = {"actors": {"users": [OLD, NEW]}}
    original = copy.deepcopy(info)
    out = remove_actor_from_policy_info(info, OLD)
    assert OLD not in out["actors"]["users"] and NEW in out["actors"]["users"]
    assert info == original


def _policy_info_body(users):
    return {
        "dataHubPolicyInfo": {
            "value": {
                "displayName": "x",
                "description": "d",
                "type": "PLATFORM",
                "state": "ACTIVE",
                "privileges": ["MANAGE_USERS"],
                "actors": {"users": list(users), "resourceOwners": False},
            }
        }
    }


class RecordingGraph:
    """Fake graph whose emit_mcp records the emitted MCP and that it was
    called -- assertions run after apply_policy_rewrite returns, not via a
    raised exception a broad except in the function could swallow."""

    def __init__(self):
        self.called = False
        self.mcp = None

    def emit_mcp(self, mcp):
        self.called = True
        self.mcp = mcp


def test_apply_policy_rewrite_dry_run_never_writes(monkeypatch):
    monkeypatch.setattr(policies, "get_entity_via_api", lambda gms_url, token, urn: _policy_info_body([OLD]))
    graph = RecordingGraph()
    result = apply_policy_rewrite(graph, "http://gms", None, POLICY_URN, NEW, dry_run=True)
    assert graph.called is False
    assert result is True


def test_apply_policy_rewrite_add_path(monkeypatch):
    # Wiring check: if the add_actor_to_policy_info call inside apply_policy_rewrite
    # were bypassed, the emitted aspect's actors.users would still be [OLD] and this
    # assertion on NEW being present would fail.
    monkeypatch.setattr(policies, "get_entity_via_api", lambda gms_url, token, urn: _policy_info_body([OLD]))
    graph = RecordingGraph()
    result = apply_policy_rewrite(graph, "http://gms", None, POLICY_URN, NEW)
    assert graph.called is True
    assert result is True
    users = graph.mcp.aspect.actors.users
    assert OLD in users and NEW in users


def test_apply_policy_rewrite_remove_path(monkeypatch):
    # Wiring check: if the remove_actor_from_policy_info call inside
    # apply_policy_rewrite were bypassed, OLD would still be present here.
    monkeypatch.setattr(policies, "get_entity_via_api", lambda gms_url, token, urn: _policy_info_body([OLD]))
    graph = RecordingGraph()
    result = apply_policy_rewrite(graph, "http://gms", None, POLICY_URN, NEW, remove_urn=OLD)
    assert graph.called is True
    assert result is True
    users = graph.mcp.aspect.actors.users
    assert OLD not in users and NEW in users


def test_apply_policy_rewrite_raises_when_policy_unfetchable(monkeypatch):
    monkeypatch.setattr(policies, "get_entity_via_api", lambda *a: None)
    with pytest.raises(RuntimeError, match=POLICY_URN):
        apply_policy_rewrite(RecordingGraph(), "http://gms", None, POLICY_URN, NEW)


def test_apply_policy_rewrite_raises_when_info_missing(monkeypatch):
    monkeypatch.setattr(policies, "get_entity_via_api", lambda *a: {"otherAspect": {"value": {}}})
    with pytest.raises(RuntimeError, match=POLICY_URN):
        apply_policy_rewrite(RecordingGraph(), "http://gms", None, POLICY_URN, NEW)


def test_apply_policy_rewrite_raises_when_value_missing(monkeypatch):
    monkeypatch.setattr(policies, "get_entity_via_api", lambda *a: {"dataHubPolicyInfo": {"value": {}}})
    with pytest.raises(RuntimeError, match=POLICY_URN):
        apply_policy_rewrite(RecordingGraph(), "http://gms", None, POLICY_URN, NEW)
