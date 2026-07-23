# tests/e2e/test_golden_utils.py
from tests.e2e.golden_utils import normalize_plan_for_golden


def _plan():
    return {
        "meta": {"tool_version": "1.0.0", "created_at": "2026-07-23T10:00:00Z",
                 "gms_url_fingerprint": "sha256:abc", "target_domain": "example-target.tld",
                 "phase": "migrate", "options": {}},
        "users": [{"old_email": "alice@example-source.tld", "changes": [{"kind": "CREATE_USER"}]}],
    }


def test_normalize_blanks_only_volatile_meta():
    out = normalize_plan_for_golden(_plan())
    assert out["meta"]["created_at"] == "<normalized>"
    assert out["meta"]["gms_url_fingerprint"] == "<normalized>"
    # everything else is untouched
    assert out["meta"]["phase"] == "migrate"
    assert out["meta"]["target_domain"] == "example-target.tld"
    assert out["users"] == _plan()["users"]


def test_normalize_does_not_mutate_input():
    p = _plan()
    normalize_plan_for_golden(p)
    assert p["meta"]["created_at"] == "2026-07-23T10:00:00Z"  # input untouched
