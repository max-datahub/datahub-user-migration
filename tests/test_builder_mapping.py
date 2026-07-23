import csv, pytest
from pathlib import Path
from dhusermig.plan import builder

def _write_csv(p: Path, rows):
    with p.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["old_email", "new_email"]); w.writerows(rows)

def test_load_mapping(tmp_path):
    p = tmp_path / "m.csv"
    _write_csv(p, [["a@src.tld", "a@dst.tld"], ["b@src.tld", "b@dst.tld"]])
    assert builder.load_mapping(p) == [("a@src.tld","a@dst.tld"), ("b@src.tld","b@dst.tld")]

def test_validate_rejects_identity_map():
    with pytest.raises(ValueError):
        builder.validate_pairs([("a@src.tld", "a@src.tld")])

def test_validate_rejects_duplicate_target():
    with pytest.raises(ValueError):
        builder.validate_pairs([("a@src.tld", "x@dst.tld"), ("b@src.tld", "x@dst.tld")])

def test_validate_rejects_malformed():
    with pytest.raises(ValueError):
        builder.validate_pairs([("not-an-email", "a@dst.tld")])

def test_validate_accepts_clean():
    builder.validate_pairs([("a@src.tld", "a@dst.tld"), ("b@src.tld", "b@dst.tld")])
