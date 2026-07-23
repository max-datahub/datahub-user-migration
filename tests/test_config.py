import pytest
from dhusermig.config import resolve_config, gms_fingerprint

def test_resolve_config_uses_explicit_over_env(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://env:8080")
    cfg = resolve_config(gms_url="http://explicit:8080", token="t")
    assert cfg.gms_url == "http://explicit:8080"
    assert cfg.token == "t"

def test_resolve_config_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://env:8080")
    monkeypatch.delenv("DATAHUB_GMS_TOKEN", raising=False)
    cfg = resolve_config(gms_url=None, token=None)
    assert cfg.gms_url == "http://env:8080"
    assert cfg.token is None

def test_resolve_config_requires_url(monkeypatch):
    monkeypatch.delenv("DATAHUB_GMS_URL", raising=False)
    with pytest.raises(ValueError):
        resolve_config(gms_url=None, token=None)

def test_fingerprint_is_stable_and_hides_url():
    fp = gms_fingerprint("https://example-source.tld/gms")
    assert fp.startswith("sha256:")
    assert "example" not in fp
    assert fp == gms_fingerprint("https://example-source.tld/gms")
