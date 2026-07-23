# tests/e2e/golden_utils.py
from __future__ import annotations

import copy
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import List, Tuple

from dhusermig.graph import get_entity_urns_owned_by_user


def normalize_plan_for_golden(plan: dict) -> dict:
    out = copy.deepcopy(plan)
    meta = out.get("meta", {})
    if "created_at" in meta:
        meta["created_at"] = "<normalized>"
    if "gms_url_fingerprint" in meta:
        meta["gms_url_fingerprint"] = "<normalized>"
    return out


def assert_plan_matches_golden(plan_path: Path, golden_path: Path, update: bool) -> None:
    normalized = normalize_plan_for_golden(json.loads(plan_path.read_text()))
    if update:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(normalized, indent=2))
        return
    golden = json.loads(golden_path.read_text())
    assert normalized == golden, f"Plan differs from golden {golden_path.name}.\nRun with --update-golden-files to refresh."


def dump_state_to_file(graph, targets: List[Tuple[str, List[str]]], out_path: Path) -> None:
    from datahub.ingestion.sink.file import write_metadata_file

    mcps = []
    for urn, aspect_names in targets:
        mcps.extend(graph.get_entity_as_mcps(urn, aspects=aspect_names))
    write_metadata_file(out_path, mcps)


def assert_state_matches_golden(
    graph,
    targets: List[Tuple[str, List[str]]],
    golden_path: Path,
    tmp_path: Path,
    update: bool,
) -> None:
    from datahub.testing.compare_metadata_json import assert_metadata_files_equal

    temp_path = tmp_path / "state_dump.json"
    dump_state_to_file(graph, targets, temp_path)
    if update:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(temp_path), str(golden_path))
        return
    assert_metadata_files_equal(
        output_path=temp_path, golden_path=golden_path, ignore_order=True
    )


def wait_until(
    predicate: Callable[[], bool],
    timeout_s: float = 120,
    interval_s: float = 3,
    sleep=time.sleep,
) -> None:
    # Elapsed is accumulated interval_s, not wall-clock, so tests with an
    # injected no-op sleep are deterministic and fast.
    elapsed = 0.0
    while True:
        if predicate():
            return
        if elapsed >= timeout_s:
            raise TimeoutError(f"condition not met within {timeout_s}s")
        sleep(interval_s)
        elapsed += interval_s


def owned_entities_settled(graph, user_urn: str, expected_urns: set[str]) -> bool:
    return set(get_entity_urns_owned_by_user(graph, user_urn)) >= expected_urns
