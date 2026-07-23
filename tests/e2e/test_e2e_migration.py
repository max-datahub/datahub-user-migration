# tests/e2e/test_e2e_migration.py
"""Golden e2e: seed -> migrate (plan+apply) -> cleanup (plan+apply), checked
against golden plan/state files plus live cross-checks. Skips without a live
GMS so the unit suite stays green locally; only runs for real in CI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datahub.emitter.mce_builder import make_user_urn
from datahub.metadata.schema_classes import DataHubPolicyInfoClass

from dhusermig.apply.policies import policies_naming_user
from dhusermig.apply.tokens import active_tokens_for_user
from dhusermig.apply.views import personal_views_for_user
from dhusermig.cli import app
from tests.e2e.golden_utils import (
    assert_plan_matches_golden,
    assert_state_matches_golden,
    owned_entities_settled,
    wait_until,
)
from tests.e2e.seed import (
    BOB_EMAIL,
    DS_MULTI_CUSTOM,
    DS_MULTI_ENUM,
    DS_OTHER,
    NEW_EMAIL,
    OLD_EMAIL,
    OTYPE_DATA_STEWARD,
    OTYPE_STEWARD,
    POLICY_URN,
    TARGET_DOMAIN,
    TOKEN_URN,
    VIEW_URN,
    seed_all,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATAHUB_GMS_URL"), reason="requires a live DataHub GMS (set DATAHUB_GMS_URL)"
)

GOLDEN = Path(__file__).parent / "golden"

runner = CliRunner()


def _invoke(*args: str):
    gms_url = os.environ["DATAHUB_GMS_URL"]
    extra = ["--gms-url", gms_url]
    token = os.environ.get("DATAHUB_GMS_TOKEN")
    if token:
        extra += ["--token", token]
    return runner.invoke(app, [*args, *extra])


def _all_settled(graph) -> bool:
    alice_urn = make_user_urn(OLD_EMAIL)
    if not owned_entities_settled(graph, alice_urn, {DS_MULTI_ENUM, DS_MULTI_CUSTOM}):
        return False
    if VIEW_URN not in personal_views_for_user(graph, alice_urn):
        return False
    if TOKEN_URN not in active_tokens_for_user(graph, alice_urn):
        return False
    if POLICY_URN not in policies_naming_user(graph, alice_urn):
        return False
    return True


def _owner_typeurns(ownership, user_urn: str) -> set:
    return {getattr(o, "typeUrn", None) for o in (ownership.owners or []) if o.owner == user_urn}


def _owners(ownership) -> set:
    return {o.owner for o in (ownership.owners or [])}


def test_migrate_then_cleanup_golden(dh_graph, tmp_path, request):
    update = request.config.getoption("--update-golden-files")

    seed_all(dh_graph)
    wait_until(lambda: _all_settled(dh_graph), timeout_s=180, interval_s=3)

    old_urn = make_user_urn(OLD_EMAIL)
    new_urn = make_user_urn(NEW_EMAIL)
    bob_urn = make_user_urn(BOB_EMAIL)

    state_targets = [
        (DS_MULTI_ENUM, ["ownership"]),
        (DS_MULTI_CUSTOM, ["ownership"]),
        (DS_OTHER, ["ownership"]),
        (POLICY_URN, ["dataHubPolicyInfo"]),
        (old_urn, ["corpUserInfo", "status"]),
        (new_urn, ["corpUserInfo", "status"]),
    ]

    # --- Migrate: plan ---
    migrate_dir = tmp_path / "migrate"
    migrate_dir.mkdir()
    result = _invoke("plan", "--user", OLD_EMAIL, "--target-domain", TARGET_DOMAIN, "--out", str(migrate_dir))
    assert result.exit_code == 0, result.output

    migrate_plan_path = migrate_dir / "plan.json"
    assert_plan_matches_golden(migrate_plan_path, GOLDEN / "migrate_plan.json", update)

    migrate_plan = json.loads(migrate_plan_path.read_text())
    # Subscriptions are Cloud-only; against OSS there's nothing to copy.
    assert all(
        change["kind"] != "COPY_SUBSCRIPTION" for user in migrate_plan["users"] for change in user["changes"]
    )

    # --- Migrate: apply ---
    result = _invoke("apply", "--plan", str(migrate_plan_path), "--yes")
    assert result.exit_code == 0, result.output

    assert_state_matches_golden(dh_graph, state_targets, GOLDEN / "post_migrate_state.json", tmp_path, update)

    # --- Migrate: live cross-checks ---
    multi_custom_ownership = dh_graph.get_ownership(DS_MULTI_CUSTOM)
    new_typeurns = _owner_typeurns(multi_custom_ownership, new_urn)
    assert OTYPE_STEWARD in new_typeurns
    assert OTYPE_DATA_STEWARD in new_typeurns
    assert old_urn in _owners(multi_custom_ownership)  # migrate keeps the old owner

    other_owners = _owners(dh_graph.get_ownership(DS_OTHER))
    assert other_owners == {bob_urn}  # unaffected by alice's migration

    result = _invoke("verify", "--plan", str(migrate_plan_path))
    assert result.exit_code == 0, result.output

    # --- Cleanup: plan ---
    cleanup_dir = tmp_path / "cleanup"
    cleanup_dir.mkdir()
    result = _invoke(
        "plan",
        "--user",
        OLD_EMAIL,
        "--target-domain",
        TARGET_DOMAIN,
        "--phase",
        "cleanup",
        "--out",
        str(cleanup_dir),
    )
    assert result.exit_code == 0, result.output

    cleanup_plan_path = cleanup_dir / "plan.json"
    assert_plan_matches_golden(cleanup_plan_path, GOLDEN / "cleanup_plan.json", update)

    # --- Cleanup: apply ---
    result = _invoke("apply", "--plan", str(cleanup_plan_path), "--yes")
    assert result.exit_code == 0, result.output

    assert_state_matches_golden(dh_graph, state_targets, GOLDEN / "post_cleanup_state.json", tmp_path, update)

    # --- Cleanup: live cross-checks ---
    assert old_urn not in _owners(dh_graph.get_ownership(DS_MULTI_ENUM))
    multi_custom_ownership = dh_graph.get_ownership(DS_MULTI_CUSTOM)
    assert old_urn not in _owners(multi_custom_ownership)
    new_typeurns = _owner_typeurns(multi_custom_ownership, new_urn)
    assert OTYPE_STEWARD in new_typeurns
    assert OTYPE_DATA_STEWARD in new_typeurns

    policy_info = dh_graph.get_aspect(POLICY_URN, DataHubPolicyInfoClass)
    assert old_urn not in (policy_info.actors.users or [])

    result = _invoke("verify", "--plan", str(cleanup_plan_path))
    assert result.exit_code == 0, result.output
