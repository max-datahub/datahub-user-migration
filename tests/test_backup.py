# tests/test_backup.py
from pathlib import Path

import pytest

from dhusermig.apply import backup


def test_run_backup_is_write_once(tmp_path: Path, monkeypatch):
    urn = "urn:li:dataset:x"
    responses = iter(["pristine pre-mutation body", "mutated post-mutation body"])
    monkeypatch.setattr(backup, "get_entity_via_api", lambda gms_url, token, u: {"body": next(responses)})

    backup.run_backup("http://gms", None, urn, "dataset", tmp_path)
    safe = backup._urn_to_safe_filename(urn)
    filepath = next(tmp_path.glob(f"{safe}.*"))
    first_contents = filepath.read_text(encoding="utf-8")
    assert "pristine" in first_contents

    # A resumed apply calling run_backup a second time for the same urn+dir
    # must not overwrite the first (pristine) backup.
    backup.run_backup("http://gms", None, urn, "dataset", tmp_path)
    second_contents = filepath.read_text(encoding="utf-8")
    # Discriminating: without the write-once guard this would contain
    # "mutated" instead, clobbering the pre-mutation state.
    assert second_contents == first_contents
    assert "mutated" not in second_contents


def test_run_backup_raises_when_fetch_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backup, "get_entity_via_api", lambda *a: None)
    with pytest.raises(backup.BackupError, match="urn:li:dataset:x"):
        backup.run_backup("http://gms", None, "urn:li:dataset:x", "dataset", tmp_path)
    assert list(tmp_path.iterdir()) == []  # no partial backup file left behind


def test_run_backup_raises_when_write_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backup, "get_entity_via_api", lambda *a: {"k": "v"})
    run_dir = tmp_path / "bk"; run_dir.mkdir(); run_dir.chmod(0o500)  # unwritable
    try:
        with pytest.raises(backup.BackupError, match="urn:li:dataset:x"):
            backup.run_backup("http://gms", None, "urn:li:dataset:x", "dataset", run_dir)
    finally:
        run_dir.chmod(0o700)
