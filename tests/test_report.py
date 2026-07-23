# tests/test_report.py
from dhusermig.plan.schema import Change, ChangeKind, Plan, PlanMeta, State, UserMigration
from dhusermig.report import write_summary


def _plan(phase: str, changes: list[Change]) -> Plan:
    return Plan(
        meta=PlanMeta(
            tool_version="1.0.0", created_at="2026-07-22T00:00:00Z",
            gms_url_fingerprint="sha256:abc", target_domain="dst.tld",
            phase=phase, options={},
        ),
        users=[UserMigration(
            old_email="a@src.tld", old_urn="urn:li:corpuser:a@src.tld",
            new_email="a@dst.tld", new_urn="urn:li:corpuser:a@dst.tld",
            changes=changes,
        )],
    )


def test_summary_lists_info_findings_and_recipe_snippet(tmp_path):
    plan = _plan("migrate", [
        Change(kind=ChangeKind.CREATE_USER, target="urn:li:corpuser:a@dst.tld"),
        Change(kind=ChangeKind.DETECT_TOKEN, target="urn:li:accessToken:t1",
               state=State.INFO, note="cannot be recreated"),
    ])
    out = tmp_path / "summary.md"
    write_summary(plan, out)
    text = out.read_text()
    assert "CREATE_USER: 1" in text
    assert "[DETECT_TOKEN] a@src.tld: urn:li:accessToken:t1 — cannot be recreated" in text
    assert "usage-based ingestion" in text  # from prevention.recipe_fix_snippet()


def test_summary_no_findings_says_none(tmp_path):
    plan = _plan("cleanup", [Change(kind=ChangeKind.DELETE_USER, target="urn:li:corpuser:a@src.tld")])
    out = tmp_path / "summary.md"
    write_summary(plan, out)
    text = out.read_text()
    assert "## Manual follow-ups\nNone." in text
