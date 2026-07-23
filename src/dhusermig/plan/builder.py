from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Optional

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import CorpUserInfoClass, OwnershipClass

from dhusermig.apply.policies import policies_naming_user
from dhusermig.apply.prevention import recreation_sources
from dhusermig.apply.tokens import active_tokens_for_user
from dhusermig.config import RunConfig, gms_fingerprint
from dhusermig.graph import (
    DiscoveryError,
    get_entity_urns_owned_by_user,
    get_graph,
    get_subscription_urns_for_user,
    user_exists_via_api,
)

from .schema import Change, ChangeKind, Plan, PlanMeta, State, UserMigration

logger = logging.getLogger(__name__)

try:
    TOOL_VERSION = _pkg_version("datahub-user-migration")
except PackageNotFoundError:
    TOOL_VERSION = "0.0.0+unknown"

CORPUSER_ENTITY = "corpuser"
BATCH_SIZE = 100
MAX_GET_OWNERSHIP_RETRIES = 3
GET_OWNERSHIP_RETRY_DELAY_S = 0.5


def _get_graph(gms_url: str, token: Optional[str] = None) -> DataHubGraph:
    config = DatahubClientConfig(server=gms_url, token=token)
    return DataHubGraph(config)


def discover_users(
    gms_url: str,
    token: Optional[str] = None,
    domain_filter: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> list[dict[str, str]]:
    """
    List all corp users. If domain_filter is set (e.g. @domain1.xyz), only include
    users whose email ends with that string. Returns list of {"urn": str, "email": str}.
    """
    graph = _get_graph(gms_url, token)
    results: list[dict[str, str]] = []
    start = 0

    while True:
        urns = graph.list_all_entity_urns(CORPUSER_ENTITY, start=start, count=BATCH_SIZE)
        if not urns:
            break
        for urn in urns:
            if not urn or "corpuser" not in urn.lower():
                continue
            email: Optional[str] = None
            try:
                info = graph.get_aspect(urn, CorpUserInfoClass)
            except Exception as e:
                # A fetch error is not "no email": in --source-domain mode it would
                # silently exclude the user from the migration. Fail loud instead.
                raise DiscoveryError(f"Could not get corpUserInfo for {urn}: {e}") from e
            if info and info.email:
                email = info.email
            if domain_filter:
                if not email or not email.endswith(domain_filter):
                    continue
            results.append({"urn": urn, "email": email or urn})
        if len(urns) < BATCH_SIZE:
            break
        start += len(urns)

    if output_path:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["urn", "email"])
            w.writeheader()
            w.writerows(results)
        logger.info("Wrote %s users to %s", len(results), output_path)

    return results


def load_mapping(mapping_file: Path) -> list[tuple[str, str]]:
    """Load (old_email, new_email) pairs from CSV. Expects header row with old_email, new_email."""
    pairs: list[tuple[str, str]] = []
    with open(mapping_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "old_email" not in (reader.fieldnames or []) or "new_email" not in (reader.fieldnames or []):
            raise ValueError("CSV must have columns: old_email, new_email")
        for row in reader:
            old = (row.get("old_email") or "").strip()
            new = (row.get("new_email") or "").strip()
            if old and new:
                pairs.append((old, new))
    return pairs


def _normalize_target_domain(domain: str) -> str:
    """Return domain without leading @ (e.g. domain2.com)."""
    return domain.lstrip("@").strip()


def _source_domain_filter(domain: str) -> str:
    """Return string suitable for email.endswith (e.g. @domain1.com)."""
    d = domain.strip().lstrip("@")
    return f"@{d}" if d else ""


def validate_pairs(pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        raise ValueError("No (old_email, new_email) pairs to migrate")
    seen_new: dict[str, str] = {}
    for old, new in pairs:
        if "@" not in old or "@" not in new:
            raise ValueError(f"Malformed email in pair: {old!r} -> {new!r}")
        if old == new:
            raise ValueError(f"old_email == new_email for {old!r}")
        if new in seen_new:
            raise ValueError(
                f"Duplicate target {new!r}: both {seen_new[new]!r} and {old!r} map to it"
            )
        seen_new[new] = old


def resolve_pairs(
    cfg: RunConfig,
    mapping_file: Optional[Path] = None,
    user: Optional[str] = None,
    target_domain: Optional[str] = None,
    source_domain: Optional[str] = None,
) -> list[tuple[str, str]]:
    """
    Build (old_email, new_email) pairs from one of:

    - mapping_file: CSV with columns old_email, new_email.
    - user + target_domain: single user firstname.lastname@domain1.com -> firstname.lastname@target_domain.
    - source_domain + target_domain: discover all users whose email ends with @source_domain,
      map each to same local part @ target_domain (uses cfg.gms_url and cfg.token).

    Returns list of (old_email, new_email). Raises ValueError if the combination of args is invalid,
    or if the resolved pairs fail validate_pairs.
    """
    if mapping_file is not None:
        pairs = load_mapping(mapping_file)
    elif user and target_domain:
        old = user.strip()
        if "@" not in old:
            raise ValueError("user must be a full email (e.g. firstname.lastname@domain1.com)")
        local = old.split("@", 1)[0]
        new = f"{local}@{_normalize_target_domain(target_domain)}"
        pairs = [(old, new)]
    elif source_domain and target_domain:
        domain_filter = _source_domain_filter(source_domain)
        discovered = discover_users(gms_url=cfg.gms_url, token=cfg.token, domain_filter=domain_filter)
        target = _normalize_target_domain(target_domain)
        pairs = []
        for u in discovered:
            email = (u.get("email") or "").strip()
            if not email or "@" not in email:
                continue
            local = email.split("@", 1)[0]
            new_email = f"{local}@{target}"
            pairs.append((email, new_email))
        if not pairs:
            logger.warning("No users found with email ending in %s", domain_filter or "(any)")
    else:
        raise ValueError(
            "Provide mapping_file, or (--user EMAIL --target-domain DOMAIN), "
            "or (--source-domain DOMAIN --target-domain DOMAIN)"
        )
    validate_pairs(pairs)
    return pairs


def owner_types_for_user(ownership: OwnershipClass, user_urn: str) -> list[list[str]]:
    """
    Return every distinct (type, typeUrn) pair user_urn holds (dedup on the full
    pair, order-stable). Deduping on type alone would collapse two CUSTOM owner
    types with distinct typeUrns down to one, silently dropping a typeUrn.
    """
    seen: set[tuple[str, str | None]] = set()
    pairs: list[list[str]] = []
    for o in ownership.owners or []:
        if o.owner != user_urn:
            continue
        key = (o.type, getattr(o, "typeUrn", None))
        if key not in seen:
            seen.add(key)
            pairs.append([key[0], key[1]])
    return pairs


# Assemble the full Plan (migrate + cleanup phases) from discovered references.


def _user_urn(email: str) -> str:
    return f"urn:li:{CORPUSER_ENTITY}:{email}"


def build_plan_from_references(
    refs: dict, phase: str, new_urn: str, old_urn: str
) -> list[Change]:
    """
    Pure: map a per-user reference dict to Change objects. No I/O, no clock reads.

    refs keys (all optional, default to no findings of that kind):
      ownership: list[(entity_urn, owner_types)]
      subscriptions: list[subscription_urn]
      policies: list[policy_urn]
      tokens: list[token_urn]                  (migrate only)
      views: list[personal_view_urn]           (migrate only)
      homepage: list[note_str]                 (migrate only)
      recreation_sources: list[source_urn]     (migrate only)
    """
    changes: list[Change] = []
    if phase == "migrate":
        changes.append(Change(kind=ChangeKind.CREATE_USER, target=new_urn))
        for entity_urn, owner_types in refs.get("ownership", []):
            changes.append(
                Change(
                    kind=ChangeKind.ADD_OWNERSHIP,
                    target=entity_urn,
                    owner_types=[list(pair) for pair in owner_types],
                )
            )
        for sub_urn in refs.get("subscriptions", []):
            changes.append(Change(kind=ChangeKind.COPY_SUBSCRIPTION, target=sub_urn))
        for policy_urn in refs.get("policies", []):
            changes.append(Change(kind=ChangeKind.REWRITE_POLICY, target=policy_urn))
        for view_urn in refs.get("views", []):
            # dataHubView has no Ownership aspect (see apply/views.py) -- detect-only.
            changes.append(
                Change(
                    kind=ChangeKind.MIGRATE_VIEW,
                    target=view_urn,
                    state=State.INFO,
                    note="personal view — detect-only; recreate under new user manually",
                )
            )
        for token_urn in refs.get("tokens", []):
            changes.append(
                Change(
                    kind=ChangeKind.DETECT_TOKEN,
                    target=token_urn,
                    state=State.INFO,
                    note=f"personal access token owned by {old_urn} — cannot be recreated for {new_urn}",
                )
            )
        for note in refs.get("homepage", []):
            changes.append(
                Change(kind=ChangeKind.DETECT_HOMEPAGE, target=old_urn, state=State.INFO, note=note)
            )
        for source_urn in refs.get("recreation_sources", []):
            changes.append(
                Change(
                    kind=ChangeKind.DETECT_RECREATION_SOURCE,
                    target=source_urn,
                    state=State.INFO,
                    note="ingestion source may recreate this user via usage extraction after migration/cleanup",
                )
            )
    elif phase == "cleanup":
        for entity_urn, _owner_types in refs.get("ownership", []):
            changes.append(Change(kind=ChangeKind.REMOVE_OWNERSHIP, target=entity_urn))
        for sub_urn in refs.get("subscriptions", []):
            changes.append(Change(kind=ChangeKind.DELETE_SUBSCRIPTION, target=sub_urn))
        for policy_urn in refs.get("policies", []):
            changes.append(Change(kind=ChangeKind.REMOVE_POLICY_ACTOR, target=policy_urn))
        changes.append(Change(kind=ChangeKind.DELETE_USER, target=old_urn))
        changes.append(Change(kind=ChangeKind.REINDEX_USER, target=old_urn))
    else:
        raise ValueError(f"Unknown phase: {phase!r}")
    # Deterministic order: discovery uses sets, so emission order varies run to
    # run. Sorting makes plan.json a stable, diffable/reviewable artifact and
    # keeps golden comparisons from flaking. Order does not affect apply (each
    # change is independent) or resume (state is tracked per change).
    changes.sort(
        key=lambda c: (
            c.kind.value,
            c.target or "",
            str(c.owner_types),
            c.source or "",
            c.field or "",
        )
    )
    return changes


def _get_ownership_with_retry(dh_graph: DataHubGraph, entity_urn: str):
    """
    Fetch ownership for entity_urn, retrying transient failures. If all retries
    fail, raises DiscoveryError -- omitting the entity would silently drop its
    ownership from the plan.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_GET_OWNERSHIP_RETRIES):
        try:
            return dh_graph.get_ownership(entity_urn)
        except Exception as e:
            last_error = e
            if attempt < MAX_GET_OWNERSHIP_RETRIES - 1:
                logger.debug(
                    "get_ownership retry %d/%d for %s: %s",
                    attempt + 1,
                    MAX_GET_OWNERSHIP_RETRIES,
                    entity_urn,
                    e,
                )
                time.sleep(GET_OWNERSHIP_RETRY_DELAY_S)
    raise DiscoveryError(
        f"get_ownership failed after {MAX_GET_OWNERSHIP_RETRIES} attempts for "
        f"{entity_urn}: {last_error}"
    ) from last_error


def _collect_references(
    dh_graph: DataHubGraph,
    old_urn: str,
    phase: str,
    recreation_source_urns: list[str],
) -> dict:
    """I/O: fetch old_urn's references from the live collectors for one user."""
    owned_urns = get_entity_urns_owned_by_user(dh_graph, old_urn)
    ownership_refs: list[tuple[str, list[list[str]]]] = []
    for entity_urn in owned_urns:
        if phase == "migrate":
            # THE multi-type fix: capture every ownership type old_urn holds on this
            # entity, not just one, so ADD_OWNERSHIP re-grants all of them.
            ownership = _get_ownership_with_retry(dh_graph, entity_urn)
            owner_types = owner_types_for_user(ownership, old_urn) if ownership else []
        else:
            owner_types = []  # REMOVE_OWNERSHIP drops by owner urn regardless of type
        ownership_refs.append((entity_urn, owner_types))

    refs: dict = {
        "ownership": ownership_refs,
        "subscriptions": get_subscription_urns_for_user(dh_graph, old_urn),
        "policies": policies_naming_user(dh_graph, old_urn),
    }
    if phase == "migrate":
        # Local import: apply.views imports owner_types_for_user back from this
        # module, so importing it at module load time would be circular.
        from dhusermig.apply.views import homepage_findings_for_user, personal_views_for_user

        refs["tokens"] = active_tokens_for_user(dh_graph, old_urn)
        refs["views"] = personal_views_for_user(dh_graph, old_urn)
        refs["homepage"] = homepage_findings_for_user(dh_graph, old_urn)
        refs["recreation_sources"] = recreation_source_urns
    return refs


def build_plan(
    cfg: RunConfig,
    pairs: list[tuple[str, str]],
    phase: str,
    options: Optional[dict] = None,
) -> Plan:
    """
    I/O: fetch live references for each (old_email, new_email) pair and assemble
    a full Plan. Aborts before making any Change if any old user is missing
    (fail fast, no partial plan).
    """
    if phase not in ("migrate", "cleanup"):
        raise ValueError(f"Unknown phase: {phase!r}")
    options = dict(options or {})

    urn_pairs = [(old, new, _user_urn(old), _user_urn(new)) for old, new in pairs]
    missing = [
        old_email
        for old_email, _new_email, old_urn, _new_urn in urn_pairs
        if not user_exists_via_api(cfg.gms_url, cfg.token, old_urn)
    ]
    if missing:
        raise ValueError(f"Old users do not exist in DataHub, aborting: {missing}")

    dh_graph = get_graph(cfg)
    recreation_source_urns = recreation_sources(dh_graph) if phase == "migrate" else []

    users: list[UserMigration] = []
    for old_email, new_email, old_urn, new_urn in urn_pairs:
        refs = _collect_references(dh_graph, old_urn, phase, recreation_source_urns)
        changes = build_plan_from_references(refs, phase=phase, new_urn=new_urn, old_urn=old_urn)
        users.append(
            UserMigration(
                old_email=old_email, old_urn=old_urn, new_email=new_email, new_urn=new_urn, changes=changes
            )
        )

    target_domain = pairs[0][1].split("@", 1)[1] if pairs and "@" in pairs[0][1] else ""
    meta = PlanMeta(
        tool_version=TOOL_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        gms_url_fingerprint=gms_fingerprint(cfg.gms_url),
        target_domain=target_domain,
        phase=phase,
        options=options,
    )
    return Plan(meta=meta, users=users)
