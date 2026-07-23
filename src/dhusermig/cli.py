# src/dhusermig/cli.py
from __future__ import annotations

import logging
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from dhusermig.apply.runner import apply_plan
from dhusermig.config import resolve_config
from dhusermig.graph import get_graph
from dhusermig.plan import store
from dhusermig.plan.builder import build_plan, resolve_pairs
from dhusermig.plan.schema import ChangeKind, State
from dhusermig.report import write_summary
from dhusermig.verify import missing_expected_ownership, remaining_old_ownership

app = typer.Typer(help="DataHub user migration toolkit (plan/apply/verify).")


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


class Phase(str, Enum):
    migrate = "migrate"
    cleanup = "cleanup"


@app.command()
def plan(
    out: Path = typer.Option(..., "--out", help="Directory to write plan.json and summary.txt"),
    mapping_file: Optional[Path] = typer.Option(
        None, "--mapping-file", exists=True, help="CSV with old_email,new_email columns"
    ),
    user: Optional[str] = typer.Option(None, "--user", help="Single user email to migrate"),
    target_domain: Optional[str] = typer.Option(None, "--target-domain"),
    source_domain: Optional[str] = typer.Option(None, "--source-domain"),
    phase: Phase = typer.Option(Phase.migrate, "--phase", help="migrate or cleanup"),
    hard: bool = typer.Option(False, "--hard", help="Use hard delete for the cleanup phase"),
    gms_url: Optional[str] = typer.Option(None, "--gms-url"),
    token: Optional[str] = typer.Option(None, "--token"),
) -> None:
    """Build a migration or cleanup plan (dry-run)."""
    try:
        cfg = resolve_config(gms_url, token)
        pairs = resolve_pairs(
            cfg,
            mapping_file=mapping_file,
            user=user,
            target_domain=target_domain,
            source_domain=source_domain,
        )
        # build_plan does I/O (fetches live refs from GMS), so it is not a pure
        # function -- it stamps its own created_at internally.
        built = build_plan(
            cfg, pairs, phase=phase.value, options={"delete_mode": "hard"} if hard else {}
        )
        plan_path = out / "plan.json"
        summary_path = out / "summary.txt"
        store.save_plan(built, plan_path)
        write_summary(built, summary_path)
    except (ValueError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo(f"Plan written to {plan_path}")
    typer.echo(f"Summary written to {summary_path}")
    total_changes = sum(len(u.changes) for u in built.users)
    typer.echo(f"Planned {total_changes} change(s) across {len(built.users)} user(s)")


@app.command()
def apply(
    plan_path: Path = typer.Option(..., "--plan", exists=True, help="Path to plan.json"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Log intended changes without applying them"),
    no_backup: bool = typer.Option(False, "--no-backup", help="Skip per-entity backup before mutating"),
    force: bool = typer.Option(False, "--force", help="Apply even if the plan's GMS fingerprint differs"),
    backup_dir: Optional[Path] = typer.Option(
        None, "--backup-dir", help="Backup directory (default: <plan dir>/backups)"
    ),
    gms_url: Optional[str] = typer.Option(None, "--gms-url"),
    token: Optional[str] = typer.Option(None, "--token"),
) -> None:
    """Apply a plan (idempotent, resumable)."""
    run_dir = backup_dir or plan_path.parent / "backups"
    try:
        cfg = resolve_config(gms_url, token)
        loaded_plan = store.load_plan(plan_path)
        done, failed = apply_plan(
            cfg,
            loaded_plan,
            plan_path,
            run_dir,
            assume_yes=yes,
            dry_run=dry_run,
            no_backup=no_backup,
            force=force,
        )
    except (ValueError, RuntimeError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo(f"done={done} failed={failed}")
    if failed:
        raise typer.Exit(1)


@app.command()
def verify(
    plan_path: Path = typer.Option(..., "--plan", exists=True, help="Path to plan.json"),
    gms_url: Optional[str] = typer.Option(None, "--gms-url"),
    token: Optional[str] = typer.Option(None, "--token"),
) -> None:
    """Verify applied state against a plan."""
    try:
        cfg = resolve_config(gms_url, token)
        loaded_plan = store.load_plan(plan_path)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    graph = get_graph(cfg)
    phase = loaded_plan.meta.phase
    any_failed = False

    for user in loaded_plan.users:
        if phase == "cleanup":
            # Cleanup removes the old owner, so the pass condition is "gone".
            targets = [
                change.target for change in user.changes if change.kind == ChangeKind.REMOVE_OWNERSHIP
            ]
            remaining = remaining_old_ownership(graph, user.old_urn, targets)
            if remaining:
                any_failed = True
                typer.echo(
                    f"FAIL {user.old_email} [cleanup]: old owner still on "
                    f"{len(remaining)} of {len(targets)} entities: {remaining}"
                )
            else:
                typer.echo(f"PASS {user.old_email} [cleanup]: no remaining ownership")
        else:
            # Migrate only ADDS the new owner (old owner stays until cleanup),
            # so the pass condition is "new user owns everything the plan added".
            expected_targets = [
                change.target for change in user.changes if change.kind == ChangeKind.ADD_OWNERSHIP
            ]
            missing = missing_expected_ownership(graph, user.new_urn, expected_targets)
            if missing:
                any_failed = True
                typer.echo(
                    f"FAIL {user.new_email} [migrate]: new owner missing "
                    f"{len(missing)} of {len(expected_targets)} expected entities: {missing}"
                )
            else:
                typer.echo(
                    f"PASS {user.new_email} [migrate]: new owner has all "
                    f"{len(expected_targets)} expected entities"
                )

    if any_failed:
        raise typer.Exit(1)


@app.command()
def status(
    plan_path: Path = typer.Option(..., "--plan", exists=True, help="Path to plan.json"),
) -> None:
    """Print plan progress."""
    try:
        loaded_plan = store.load_plan(plan_path)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    counts: Counter = Counter()
    for user in loaded_plan.users:
        for change in user.changes:
            counts[(change.kind, change.state)] += 1

    typer.echo(f"Phase: {loaded_plan.meta.phase}")
    typer.echo(f"Users: {len(loaded_plan.users)}")
    for kind in ChangeKind:
        by_state = {state.value: counts[(kind, state)] for state in State if counts[(kind, state)]}
        if by_state:
            typer.echo(f"{kind.value}: " + ", ".join(f"{s}={c}" for s, c in by_state.items()))


if __name__ == "__main__":
    app()
