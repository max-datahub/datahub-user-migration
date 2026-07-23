# tests/test_appliers_raise.py
import urllib.request

import pytest

from dhusermig.apply import prevention, users

OLD = "urn:li:corpuser:a@src.tld"
NEW = "urn:li:corpuser:a@dst.tld"


def test_create_new_user_raises_on_empty_aspects():
    class EmptyGraph:
        def get_entity_as_mcps(self, urn, aspects=None):
            return []

    with pytest.raises(RuntimeError, match=OLD):
        users.create_new_user_from_old(EmptyGraph(), OLD, NEW)


def test_reindex_user_raises_on_http_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match=OLD):
        prevention.reindex_user("http://gms", None, OLD)
