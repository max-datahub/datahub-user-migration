from datahub.metadata.schema_classes import OwnerClass, OwnershipClass

from dhusermig.apply.views import build_view_ownership_repoint

OLD = "urn:li:corpuser:a@src.tld"
NEW = "urn:li:corpuser:a@dst.tld"


def test_view_repoint_replaces_owner():
    own = OwnershipClass(owners=[OwnerClass(owner=OLD, type="TECHNICAL_OWNER")])
    out = build_view_ownership_repoint(own, OLD, NEW)
    owners = {(o.owner, o.type) for o in out.owners}
    assert (NEW, "TECHNICAL_OWNER") in owners
    assert (OLD, "TECHNICAL_OWNER") not in owners
