from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import SystemMetadataClass

from dhusermig.apply import backup, ownership, policies, prevention, subscriptions, users
from dhusermig.config import RunConfig, gms_fingerprint
from dhusermig.graph import get_graph
from dhusermig.plan.schema import Change, ChangeKind, Plan, State, UserMigration
from dhusermig.plan import store

logger = logging.getLogger(__name__)

RUN_ID = "user-migration"


def _entity_type_from_urn(urn: str) -> str:
    """Extract the entity type segment from a `urn:li:<type>:...` URN."""
    parts = urn.split(":", 3)
    return parts[2] if len(parts) > 2 else "unknown"


def _delete_mode_is_hard(plan: Plan) -> bool:
    return (plan.meta.options or {}).get("delete_mode") == "hard"


_CLEANUP_DESTRUCTIVE_KINDS = (
    ChangeKind.DELETE_USER,
    ChangeKind.DELETE_SUBSCRIPTION,
    ChangeKind.REMOVE_OWNERSHIP,
)

# Backup-exempt kinds: CREATE_USER's target is the NEW user urn, which does not
# exist yet (rollback for a created user is deleting it); REINDEX_USER is an
# index-only operation with no primary-storage mutation (and its target may
# already be hard-deleted by the time cleanup reindexes it).
_BACKUP_EXEMPT_KINDS = (ChangeKind.CREATE_USER, ChangeKind.REINDEX_USER)


def _cleanup_destructive_counts(plan: Plan) -> str:
    """Human-readable counts of pending destructive cleanup-phase changes."""
    counts = Counter(change.kind for _, change in store.pending_changes(plan))
    parts = [f"{kind.value}={counts[kind]}" for kind in _CLEANUP_DESTRUCTIVE_KINDS if counts[kind]]
    return ", ".join(parts) or "none"


def dispatch(
    cfg: RunConfig,
    plan: Plan,
    change: Change,
    user: UserMigration,
    run_dir: Path,
    dry_run: bool,
) -> None:
    """
    Route a Change to its applier by kind. INFO kinds never reach here --
    store.pending_changes only yields PENDING changes.

    dispatch()'s signature is fixed (apply_plan's tests monkeypatch it directly),
    so per-run state that doesn't fit those params -- the shared DataHubGraph and
    the no_backup flag -- rides along on `cfg` instead of widening the signature.
    """
    graph = getattr(cfg, "graph", None) or get_graph(cfg)
    no_backup = getattr(cfg, "no_backup", False)
    if not no_backup and not dry_run and change.kind not in _BACKUP_EXEMPT_KINDS:
        backup.run_backup(
            cfg.gms_url, cfg.token, change.target, _entity_type_from_urn(change.target), run_dir
        )

    kind = change.kind
    if kind == ChangeKind.CREATE_USER:
        users.create_new_user_from_old(graph, user.old_urn, user.new_urn, user.new_email, dry_run)
    elif kind == ChangeKind.ADD_OWNERSHIP:
        own = graph.get_ownership(change.target)
        new_own = ownership.build_ownership_with_owner(own, user.new_urn, change.owner_types)
        if dry_run:
            logger.info("Dry run: would update ownership on %s", change.target)
        else:
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=change.target, aspect=new_own, systemMetadata=SystemMetadataClass(runId=RUN_ID)
                )
            )
    elif kind == ChangeKind.COPY_SUBSCRIPTION:
        # get_existing_subscription_inputs must run once per user, not once per
        # subscription -- cache it on cfg (persists across dispatch calls within
        # one apply_plan run) keyed by new_urn.
        sub_cache = getattr(cfg, "_sub_cache", None)
        if sub_cache is None:
            sub_cache = cfg._sub_cache = {}
        if user.new_urn not in sub_cache:
            sub_cache[user.new_urn] = subscriptions.get_existing_subscription_inputs(
                graph, cfg.gms_url, cfg.token, user.new_urn
            )
        created = subscriptions.copy_subscription(
            graph, cfg.gms_url, cfg.token, change.target, user.new_urn, sub_cache[user.new_urn], dry_run
        )
        if not created:  # benign dedupe skip -- real failures raise
            logger.info(
                "Skipped subscription %s: %s already subscribed", change.target, user.new_urn
            )
    elif kind == ChangeKind.REWRITE_POLICY:
        policies.apply_policy_rewrite(
            graph, cfg.gms_url, cfg.token, change.target, user.new_urn, remove_urn=None, dry_run=dry_run
        )
    elif kind == ChangeKind.REMOVE_OWNERSHIP:
        own = graph.get_ownership(change.target)
        new_own = ownership.build_ownership_without_owner(own, user.old_urn)
        if dry_run:
            logger.info("Dry run: would remove ownership on %s", change.target)
        else:
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=change.target, aspect=new_own, systemMetadata=SystemMetadataClass(runId=RUN_ID)
                )
            )
    elif kind == ChangeKind.DELETE_SUBSCRIPTION:
        subscriptions.delete_subscription(graph, change.target, dry_run, hard=_delete_mode_is_hard(plan))
    elif kind == ChangeKind.REMOVE_POLICY_ACTOR:
        policies.apply_policy_rewrite(
            graph, cfg.gms_url, cfg.token, change.target,
            new_urn=user.new_urn, remove_urn=user.old_urn, dry_run=dry_run,
        )
    elif kind == ChangeKind.DELETE_USER:
        if dry_run:
            logger.info("Dry run: would %s-delete user %s", "hard" if _delete_mode_is_hard(plan) else "soft", user.old_urn)
        elif _delete_mode_is_hard(plan):
            graph.hard_delete_entity(user.old_urn)
        else:
            graph.soft_delete_entity(user.old_urn)
    elif kind == ChangeKind.REINDEX_USER:
        prevention.reindex_user(cfg.gms_url, cfg.token, user.old_urn, dry_run)
    else:
        raise ValueError(f"No applier routed for change kind: {kind}")


def apply_plan(
    cfg: RunConfig,
    plan: Plan,
    plan_path: Path,
    run_dir: Path,
    assume_yes: bool = False,
    dry_run: bool = False,
    no_backup: bool = False,
    force: bool = False,
) -> tuple[int, int]:
    if not force and plan.meta.gms_url_fingerprint != gms_fingerprint(cfg.gms_url):
        raise RuntimeError(
            "GMS fingerprint mismatch: plan was built against a different instance "
            "(pass --force to override)"
        )
    pending = list(store.pending_changes(plan))
    if not assume_yes and not dry_run:
        if plan.meta.phase == "cleanup":
            resp = input(
                f"DESTRUCTIVE CLEANUP: apply {len(pending)} pending change(s) "
                f"({_cleanup_destructive_counts(plan)}) to {cfg.gms_url}? [y/N] "
            )
            if resp.strip().lower() != "y":
                raise RuntimeError("aborted by user")
            if _delete_mode_is_hard(plan):
                resp2 = input(
                    "Hard delete is IRREVERSIBLE. Type 'yes' to confirm hard delete: "
                )
                if resp2.strip().lower() != "yes":
                    raise RuntimeError("aborted by user (hard-delete confirmation)")
        else:
            resp = input(f"Apply {len(pending)} pending change(s) to {cfg.gms_url}? [y/N] ")
            if resp.strip().lower() != "y":
                raise RuntimeError("aborted by user")
    if no_backup and not dry_run:
        # Deliberately NOT bypassed by assume_yes: running without per-entity
        # backups is an explicit opt-out of the safety net.
        resp = input(
            "WARNING: --no-backup will skip per-entity backups; rollback will be "
            "IMPOSSIBLE. Type 'no-backup' to proceed: "
        )
        if resp.strip().lower() != "no-backup":
            raise RuntimeError("aborted (no-backup confirmation)")

    # dispatch()'s signature can't carry extra params (tests monkeypatch it
    # verbatim) -- thread the run-scoped graph + no_backup flag through cfg.
    # All three run-scoped fields (graph, no_backup, _sub_cache) must be reset
    # together here -- a reused RunConfig would otherwise dedup subscriptions
    # against a stale cache from a prior apply_plan call.
    cfg.graph = get_graph(cfg)
    cfg.no_backup = no_backup
    cfg._sub_cache = {}

    marker = " [dry-run]" if dry_run else ""
    total = len(pending)
    logger.info(
        "Applying plan: phase=%s users=%d pending=%d gms=%s%s",
        plan.meta.phase, len(plan.users), total, cfg.gms_url, marker,
    )
    done = failed = 0
    for i, (user, change) in enumerate(pending, start=1):
        logger.info(
            "[%d/%d]%s %s %s (%s -> %s)",
            i, total, marker, change.kind.value, change.target, user.old_email, user.new_email,
        )
        try:
            dispatch(cfg, plan, change, user, run_dir, dry_run)
            if not dry_run:  # dry-run is fully read-only: never persist state
                store.set_state(plan, change, State.DONE, plan_path)
            done += 1
        except Exception as e:  # per-change fault isolation
            logger.error("[%d/%d] %s %s failed: %s", i, total, change.kind.value, change.target, e)
            if not dry_run:
                store.set_state(plan, change, State.FAILED, plan_path, error=str(e))
            failed += 1
    if dry_run:
        logger.info(
            "Dry run complete: %d change(s) would apply, %d failed; no writes were "
            "made and the plan file was not modified.", done, failed,
        )
    else:
        logger.info("Apply complete: done=%d failed=%d", done, failed)
    return done, failed
