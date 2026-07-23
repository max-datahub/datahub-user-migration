from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dhusermig.graph import get_entity_via_api

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """A per-entity backup could not be taken; the entity must not be mutated."""


def _urn_to_safe_filename(urn: str, max_len: int = 180) -> str:
    """Return a safe filename suffix from URN (no path separators, bounded length)."""
    safe = re.sub(r"[^a-zA-Z0-9\-_.]", "_", urn)
    if len(safe) > max_len:
        h = hashlib.sha256(urn.encode()).hexdigest()[:16]
        safe = safe[: max_len - 20] + "_" + h
    return safe or "entity"


def run_backup(
    gms_url: str,
    token: Optional[str],
    urn: str,
    entity_type: str,
    run_dir: Path,
) -> None:
    """
    Back up a single entity to run_dir/{safe_urn}.yaml (or .json if PyYAML is
    unavailable) before the runner performs its first mutation for that entity.
    The plan is the manifest, so no separate manifest.csv is written here.

    Raises BackupError if the entity cannot be fetched or the backup cannot be
    written -- no backup means no mutation.
    """
    try:
        import yaml as _yaml
        use_yaml = True
    except ImportError:
        use_yaml = False

    run_dir.mkdir(parents=True, exist_ok=True)
    safe = _urn_to_safe_filename(urn)
    ext = "yaml" if use_yaml else "json"
    filepath = run_dir / f"{safe}.{ext}"

    if filepath.exists():
        # Write-once: a resumed apply must never clobber a pristine
        # pre-mutation backup with post-mutation state.
        return

    body = get_entity_via_api(gms_url, token, urn)
    if not body:
        raise BackupError(f"Could not fetch entity for backup: {urn}")
    header = {
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "entity_urn": urn,
        "entity_type": entity_type,
    }
    out = {"_backup_meta": header, "entity": body}
    try:
        if use_yaml:
            with filepath.open("w", encoding="utf-8") as f:
                _yaml.dump(out, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        else:
            with filepath.open("w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
    except Exception as e:
        raise BackupError(f"Could not write backup for {urn} at {filepath}: {e}") from e
