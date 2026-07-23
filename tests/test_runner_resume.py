# tests/test_runner_resume.py
from pathlib import Path

import pytest

import dhusermig.apply.runner as runner
from dhusermig.plan.schema import Change, ChangeKind, Plan, PlanMeta, State, UserMigration
from dhusermig.plan import store
from dhusermig.config import RunConfig, gms_fingerprint
from tests.test_schema import _sample_plan


def _cleanup_plan(hard: bool) -> Plan:
    return Plan(
        meta=PlanMeta(
            tool_version="1.0.0", created_at="2026-07-22T00:00:00Z",
            gms_url_fingerprint=gms_fingerprint("http://gms:8080"),
            target_domain="example-target.tld", phase="cleanup",
            options={"delete_mode": "hard"} if hard else {},
        ),
        users=[UserMigration(
            old_email="a@example-source.tld", old_urn="urn:li:corpuser:a@example-source.tld",
            new_email="a@example-target.tld", new_urn="urn:li:corpuser:a@example-target.tld",
            changes=[
                Change(kind=ChangeKind.REMOVE_OWNERSHIP, target="urn:li:dataset:x"),
                Change(kind=ChangeKind.DELETE_USER, target="urn:li:corpuser:a@example-source.tld"),
            ],
        )],
    )

def test_apply_skips_done_and_isolates_failures(tmp_path: Path, monkeypatch):
    plan = _sample_plan()
    plan.meta.gms_url_fingerprint = gms_fingerprint("http://gms:8080")
    plan.users[0].changes[0].state = State.DONE          # already done -> skipped
    p = tmp_path / "plan.json"; store.save_plan(plan, p)

    calls = []
    def fake_dispatch(cfg, plan, change, user, run_dir, dry_run):
        calls.append(change.kind)
        if change.kind.value == "ADD_OWNERSHIP":
            raise RuntimeError("boom")
    monkeypatch.setattr(runner, "dispatch", fake_dispatch)

    monkeypatch.setattr("builtins.input", lambda *_: "no-backup")
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    done, failed = runner.apply_plan(cfg, plan, p, tmp_path / "bk",
                                     assume_yes=True, no_backup=True)
    # CREATE_USER was DONE (skipped); ADD_OWNERSHIP attempted+failed; DETECT_TOKEN is INFO (skipped)
    assert done == 0 and failed == 1
    reloaded = store.load_plan(p)
    assert reloaded.users[0].changes[1].state == State.FAILED
    assert "boom" in (reloaded.users[0].changes[1].error or "")

def test_apply_plan_resets_sub_cache_between_runs(tmp_path: Path, monkeypatch):
    plan = _sample_plan()
    plan.meta.gms_url_fingerprint = gms_fingerprint("http://gms:8080")
    p = tmp_path / "plan.json"; store.save_plan(plan, p)

    def fake_dispatch(cfg, plan, change, user, run_dir, dry_run):
        pass  # no-op: must not repopulate _sub_cache
    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr("builtins.input", lambda *_: "no-backup")

    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    cfg._sub_cache = {"stale_user": [{"entityUrn": "urn:li:dataset:old"}]}
    runner.apply_plan(cfg, plan, p, tmp_path / "bk", assume_yes=True, no_backup=True)
    assert cfg._sub_cache == {}

def test_apply_refuses_on_fingerprint_mismatch(tmp_path: Path):
    plan = _sample_plan()
    plan.meta.gms_url_fingerprint = gms_fingerprint("http://OTHER:8080")
    p = tmp_path / "plan.json"; store.save_plan(plan, p)
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    try:
        runner.apply_plan(cfg, plan, p, tmp_path / "bk", assume_yes=True, no_backup=True)
        assert False, "expected refusal"
    except RuntimeError as e:
        assert "fingerprint" in str(e).lower()

def test_cleanup_hard_apply_refuses_without_second_confirmation(tmp_path: Path, monkeypatch):
    plan = _cleanup_plan(hard=True)
    p = tmp_path / "plan.json"; store.save_plan(plan, p)
    monkeypatch.setattr(runner, "dispatch", lambda *a, **k: None)
    # First (destructive-cleanup) prompt accepted; second (hard-delete) prompt declined.
    responses = iter(["y", "no"])
    monkeypatch.setattr("builtins.input", lambda *_: next(responses))
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    with pytest.raises(RuntimeError):
        runner.apply_plan(cfg, plan, p, tmp_path / "bk", assume_yes=False, no_backup=True)

def test_cleanup_hard_apply_proceeds_with_assume_yes(tmp_path: Path, monkeypatch):
    plan = _cleanup_plan(hard=True)
    p = tmp_path / "plan.json"; store.save_plan(plan, p)
    calls = []
    monkeypatch.setattr(runner, "dispatch", lambda cfg, plan, change, user, run_dir, dry_run: calls.append(change.kind))
    # assume_yes skips the apply/hard-delete prompts, but NOT the no-backup one.
    monkeypatch.setattr("builtins.input", lambda *_: "no-backup")
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    done, failed = runner.apply_plan(cfg, plan, p, tmp_path / "bk", assume_yes=True, no_backup=True)
    assert done == 2 and failed == 0
    assert len(calls) == 2

def test_no_backup_requires_typed_confirmation(tmp_path: Path, monkeypatch):
    plan = _sample_plan()
    plan.meta.gms_url_fingerprint = gms_fingerprint("http://gms:8080")
    p = tmp_path / "plan.json"; store.save_plan(plan, p)
    monkeypatch.setattr(runner, "dispatch", lambda *a, **k: None)
    # Anything other than the exact token "no-backup" must abort -- even with --yes.
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    with pytest.raises(RuntimeError, match="no-backup confirmation"):
        runner.apply_plan(cfg, plan, p, tmp_path / "bk", assume_yes=True, no_backup=True)

def test_dry_run_never_persists_state(tmp_path: Path, monkeypatch):
    plan = _sample_plan()
    plan.meta.gms_url_fingerprint = gms_fingerprint("http://gms:8080")
    p = tmp_path / "plan.json"; store.save_plan(plan, p)
    before = p.read_text(encoding="utf-8")

    def fake_dispatch(cfg, plan, change, user, run_dir, dry_run):
        if change.kind == ChangeKind.ADD_OWNERSHIP:
            raise RuntimeError("boom")
    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    done, failed = runner.apply_plan(cfg, plan, p, tmp_path / "bk", dry_run=True, no_backup=True)
    assert done == 1 and failed == 1
    # No writes: plan file byte-identical, in-memory states untouched.
    assert p.read_text(encoding="utf-8") == before
    assert plan.users[0].changes[0].state == State.PENDING
    assert plan.users[0].changes[1].state == State.PENDING
    assert plan.users[0].changes[1].error is None

def test_dispatch_dry_run_skips_backup(tmp_path: Path, monkeypatch):
    called = []
    monkeypatch.setattr(runner.backup, "run_backup", lambda *a, **k: called.append(a))
    monkeypatch.setattr(runner.policies, "apply_policy_rewrite", lambda *a, **k: True)
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    cfg.graph = object()
    plan = _sample_plan(); user = plan.users[0]
    ch = Change(kind=ChangeKind.REWRITE_POLICY, target="urn:li:dataHubPolicy:1")
    runner.dispatch(cfg, plan, ch, user, tmp_path, dry_run=True)
    assert called == []
    runner.dispatch(cfg, plan, ch, user, tmp_path, dry_run=False)  # control
    assert len(called) == 1

def test_dispatch_exempts_create_user_and_reindex_from_backup(tmp_path: Path, monkeypatch):
    called = []
    monkeypatch.setattr(runner.backup, "run_backup", lambda *a, **k: called.append(a))
    monkeypatch.setattr(runner.users, "create_new_user_from_old", lambda *a, **k: 1)
    monkeypatch.setattr(runner.prevention, "reindex_user", lambda *a, **k: True)
    cfg = RunConfig(gms_url="http://gms:8080", token=None)
    cfg.graph = object()
    plan = _sample_plan(); user = plan.users[0]
    runner.dispatch(cfg, plan, Change(kind=ChangeKind.CREATE_USER, target=user.new_urn),
                    user, tmp_path, dry_run=False)
    runner.dispatch(cfg, plan, Change(kind=ChangeKind.REINDEX_USER, target=user.old_urn),
                    user, tmp_path, dry_run=False)
    assert called == []
