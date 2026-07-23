# tests/test_store.py
from pathlib import Path
from dhusermig.plan.schema import State
from dhusermig.plan import store
from tests.test_schema import _sample_plan

def test_save_load_roundtrip(tmp_path: Path):
    p = tmp_path / "plan.json"
    plan = _sample_plan()
    store.save_plan(plan, p)
    assert store.load_plan(p) == plan

def test_pending_changes_excludes_info_and_done(tmp_path: Path):
    plan = _sample_plan()
    pend = list(store.pending_changes(plan))
    # sample has CREATE_USER (pending), ADD_OWNERSHIP (pending), DETECT_TOKEN (info)
    assert len(pend) == 2
    assert all(c.state == State.PENDING for _, c in pend)

def test_pending_changes_includes_failed(tmp_path: Path):
    # A resumed run must retry FAILED changes, not just PENDING ones.
    plan = _sample_plan()
    plan.users[0].changes[1].state = State.FAILED  # ADD_OWNERSHIP
    pend = list(store.pending_changes(plan))
    # Discriminating: if pending_changes only yielded PENDING, this would be
    # length 1 (just CREATE_USER) and the FAILED change would never surface.
    assert len(pend) == 2
    states = {c.state for _, c in pend}
    assert states == {State.PENDING, State.FAILED}

def test_set_state_persists(tmp_path: Path):
    p = tmp_path / "plan.json"
    plan = _sample_plan()
    store.save_plan(plan, p)
    change = plan.users[0].changes[0]
    store.set_state(plan, change, State.DONE, p)
    reloaded = store.load_plan(p)
    assert reloaded.users[0].changes[0].state == State.DONE
