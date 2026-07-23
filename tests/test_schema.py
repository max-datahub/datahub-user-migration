# tests/test_schema.py
from dhusermig.plan.schema import (
    Change, ChangeKind, Plan, PlanMeta, State, UserMigration,
)

def _sample_plan() -> Plan:
    return Plan(
        meta=PlanMeta(
            tool_version="1.0.0", created_at="2026-07-22T00:00:00Z",
            gms_url_fingerprint="sha256:abc", target_domain="example-target.tld",
            phase="migrate", options={"delete_mode": "soft"},
        ),
        users=[UserMigration(
            old_email="a@example-source.tld", old_urn="urn:li:corpuser:a@example-source.tld",
            new_email="a@example-target.tld", new_urn="urn:li:corpuser:a@example-target.tld",
            changes=[
                Change(kind=ChangeKind.CREATE_USER, target="urn:li:corpuser:a@example-target.tld"),
                Change(kind=ChangeKind.ADD_OWNERSHIP, target="urn:li:dataset:x",
                       owner_types=[["BUSINESS_OWNER", None], ["TECHNICAL_OWNER", None]]),
                Change(kind=ChangeKind.DETECT_TOKEN, target="urn:li:token:1",
                       state=State.INFO, note="manual recreate"),
            ],
        )],
    )

def test_plan_roundtrip():
    plan = _sample_plan()
    restored = Plan.from_dict(plan.to_dict())
    assert restored == plan
    # owner_types survives round-trip as (type, typeUrn) pairs, both entries
    add = restored.users[0].changes[1]
    assert add.owner_types == [["BUSINESS_OWNER", None], ["TECHNICAL_OWNER", None]]

def test_enums_serialize_as_strings():
    d = _sample_plan().to_dict()
    assert d["users"][0]["changes"][0]["kind"] == "CREATE_USER"
    assert d["users"][0]["changes"][2]["state"] == "INFO"
