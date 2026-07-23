# datahub-user-migration

A plan/apply/verify CLI for migrating DataHub `CorpUser` identities when a user's
login email changes (for example, an email domain rename). A DataHub user URN is
derived from the user's email (`urn:li:corpuser:<email>`), so a domain change does
not rename the existing user — it mints a brand-new, disconnected URN. Everything
anchored to the old URN (ownership, subscriptions, group and role membership,
access policies, personal views, homepage personalization, access tokens) stays
behind unless it is explicitly re-pointed. This tool builds a reviewable,
resumable plan for that re-pointing, applies it, and verifies the result.

**Prevention is preferable to migration.** If you control the IdP configuration,
configure a stable, domain-independent username claim (a unique user ID rather
than the email address) as the identifier DataHub uses for the corpuser URN, so
this problem does not recur. See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) before
running this tool against a production instance.

## Requirements and installation

- Python 3.10 or later
- A DataHub Cloud instance (subscription discovery and deletion use DataHub
  Cloud APIs; other operations use standard DataHub APIs)

```bash
pip install -e .
```

This installs `acryl-datahub>=0.13` (the DataHub Python SDK), `typer` (CLI), and
`PyYAML` (backup file format), and exposes a `datahub-user-migration` console
script (`dhusermig.cli:app`). The examples below also work as
`python -m dhusermig.cli`.

## Authentication

Every command accepts `--gms-url`/`--token`, or reads from the environment:

```bash
export DATAHUB_GMS_URL=https://<your-instance>.acryl.io/gms
export DATAHUB_GMS_TOKEN=<personal-access-token>
```

An explicit `--gms-url`/`--token` flag always takes precedence over the
environment variables. The token must belong to a user with sufficient privileges
to read and edit entities, users, policies, and subscriptions.

## Quick start

A migration is two plan/apply/verify passes: `migrate` (create the new user, copy
everything forward, leave the old user in place) and `cleanup` (strip the old
user out once the new one is confirmed fully working).

```bash
# 1. Build the migrate-phase plan (read-only: no writes to DataHub)
datahub-user-migration plan \
  --user a@example-source.tld --target-domain example-target.tld \
  --out out/migrate
# On success, plan prints "Planned N change(s) across M user(s)".

# 2. Review out/migrate/plan.json (the full change list + resume state) and
#    out/migrate/summary.txt (human-readable counts + a "Manual follow-ups"
#    section for anything the tool only detects, e.g. tokens/views/homepage)

# 3. Apply it (prompts for confirmation unless --yes; backs up every entity
#    before its first mutation)
datahub-user-migration apply --plan out/migrate/plan.json

# 4. Verify: confirms the new user now owns everything the migrate plan added
#    (verify is phase-aware -- see docs/RUNBOOK.md for what it checks per phase)
datahub-user-migration verify --plan out/migrate/plan.json

# 5. Once the new user is confirmed working, build the cleanup plan
datahub-user-migration plan \
  --user a@example-source.tld --target-domain example-target.tld \
  --phase cleanup --out out/cleanup

# 6. Apply cleanup (removes old ownership/subscriptions/policy actors, then
#    deletes the old user and reindexes it)
datahub-user-migration apply --plan out/cleanup/plan.json

# 7. Verify: this should now report PASS for every user
datahub-user-migration verify --plan out/cleanup/plan.json
```

Instead of `--user`/`--target-domain` for a single account, `plan` also accepts:

- `--mapping-file some.csv` — a CSV with `old_email,new_email` columns, for an
  arbitrary list of pairs.
- `--source-domain example-source.tld --target-domain example-target.tld` —
  discover every user under the source domain and migrate all of them, keeping
  the same local part.

## Command reference

A global `--verbose`/`-v` flag (placed before the subcommand, e.g.
`datahub-user-migration -v apply ...`) enables DEBUG-level logging. By default,
`apply` logs an INFO-level run header, a `[i/N] KIND target (old -> new)` line
per change, an ERROR line per failure, and an end-of-run summary.

**`plan`** — build a migrate or cleanup plan. Read-only: nothing is written to
DataHub. Discovery is fail-fast: if any discovery step fails (the ownership
relationship walk and its search fallback both failing, or subscription, policy,
token, view, homepage, or ingestion-source listing failing), plan building aborts
with a `DiscoveryError` rather than silently producing a partial plan. Fix the
connectivity or permission issue and re-run `plan`.

| Flag | Meaning |
| --- | --- |
| `--out PATH` (required) | Directory to write `plan.json` + `summary.txt` |
| `--mapping-file PATH` | CSV with `old_email,new_email` columns |
| `--user EMAIL` | Single user, paired with `--target-domain` |
| `--target-domain DOMAIN` | New domain for `--user` or `--source-domain` |
| `--source-domain DOMAIN` | Discover + migrate every user under this domain |
| `--phase migrate\|cleanup` | Default `migrate` |
| `--hard` | Cleanup phase only: hard-delete instead of soft-delete |
| `--gms-url`, `--token` | Override env vars |

Exactly one of `--mapping-file`, `--user`+`--target-domain`, or
`--source-domain`+`--target-domain` must be given.

**`apply`** — apply a plan. Idempotent and resumable: re-running it against the
same `plan.json` skips changes already marked `DONE` and retries only
`PENDING`/`FAILED` ones. Each entity is backed up before its first mutation; if
a backup cannot be taken, that change is marked `FAILED` and the entity is not
mutated (see [Safety model](#safety-model)).

| Flag | Meaning |
| --- | --- |
| `--plan PATH` (required) | Path to `plan.json` |
| `--yes` | Skip the confirmation prompt (does not bypass the `--no-backup` confirmation) |
| `--dry-run` | Fully read-only preview: no writes to DataHub, no backup files, and `plan.json` is not modified |
| `--no-backup` | Skip per-entity backups. Requires typing `no-backup` at an interactive confirmation prompt; `--yes` does not bypass it. Not recommended |
| `--force` | Apply even if the plan's GMS fingerprint doesn't match `--gms-url` |
| `--backup-dir PATH` | Where backups go (default: `<plan dir>/backups`) |
| `--gms-url`, `--token` | Override env vars |

**`verify`** — check applied state against a plan. Phase-aware and scoped to the
plan's own targets:

- Against a **migrate** plan, it checks that the new user owns every target of
  the plan's `ADD_OWNERSHIP` changes.
- Against a **cleanup** plan, it checks that the old user no longer owns the
  targets of the plan's `REMOVE_OWNERSHIP` changes.

Both checks read the Ownership aspect from primary storage for each target, so
they are not subject to search-index lag. Verification does not sweep the whole
instance, and it does not verify subscriptions, policies, or the user clone. See
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) for details.

| Flag | Meaning |
| --- | --- |
| `--plan PATH` (required) | Path to `plan.json` |
| `--gms-url`, `--token` | Override env vars |

**`status`** — print per-kind, per-state change counts for a plan. Takes only
`--plan PATH`. Reads only the local plan file; never contacts DataHub.

## Scope and limitations

### Automated

- **Ownership** — every ownership type the old user holds (including custom
  ownership types with their type URNs) is re-granted to the new user during
  migrate, and the old owner is removed during cleanup. Owned entities are
  discovered via the `OwnedBy` relationship index with a search-by-owner
  fallback, so coverage is entity-type-agnostic: datasets, dashboards, charts,
  data flows and jobs, containers, domains, glossary terms and nodes, tags, data
  products, ML assets, notebooks, groups, and any other ownership-bearing entity
  type.
- **Subscriptions** — the old user's subscriptions are recreated for the new
  user via the DataHub Cloud API (with deduplication, so re-running `apply`
  cannot double-create them) and removed during cleanup.
- **Group and role membership** — the new user is created as an 8-aspect clone
  of the old user (`corpUserInfo`, `corpUserEditableInfo`, `groupMembership`,
  `nativeGroupMembership`, `status`, `roleMembership`, `corpUserStatus`,
  `corpUserSettings`), so group and role membership carry over as part of the
  clone.
- **Access-policy actors** — policies naming the old user in `actors.users` gain
  the new user during migrate; the old user is stripped during cleanup (two
  phases, so access is never dropped mid-migration).
- **Old-user removal and reindex (cleanup)** — the old user is soft-deleted
  (hard-deleted with `--hard`) and its search index is rebuilt from primary
  storage so it does not linger in search results.

### Detected and reported only (manual follow-up required)

These are listed in the plan and in the "Manual follow-ups" section of
`summary.txt`, but are not modified by the tool:

- **Personal access tokens** — tokens cannot be minted on behalf of another
  user; the new user must generate replacements and update any consumers.
- **Personal views** — DataHub views carry no re-pointable owner reference; the
  view must be recreated under the new user.
- **Homepage personalization** — the old user's homepage template, dismissed
  announcements, and default view are reported for manual reproduction.
- **Usage-based ingestion sources** — sources whose usage extraction can
  recreate the old user from query history are flagged, with a recipe fix
  included in `summary.txt` (see [`docs/RUNBOOK.md`](docs/RUNBOOK.md)).

### Out of scope

- **Historical audit and timeline metadata** — immutable by design; the old
  email continues to appear in audit trails and `created`/`lastModified` actor
  stamps from before the migration.
- **Form assignments** and **incident assignees**.
- **`createdBy`/actor stamps** on queries, posts, and similar entities.
- Any DataHub Cloud actor reference not listed in the sections above.

## Safety model

- **Read-only planning.** `plan` never writes to DataHub; it produces
  `plan.json` and `summary.txt` for review before anything is applied.
- **Fail-fast discovery.** Any failed discovery step aborts plan building with a
  `DiscoveryError` instead of producing a silently incomplete plan.
- **Dry-run guarantee.** `apply --dry-run` is fully read-only: no writes to
  DataHub, no backup files are created, and the `plan.json` state file is not
  modified.
- **Mandatory pre-mutation backups.** Backups are on by default and hard-fail:
  each entity's full body is backed up before its first mutation in a run; if
  the backup cannot be taken (fetch or write failure), that change is marked
  `FAILED` and the entity is not mutated — a resumed `apply` retries it.
  (`CREATE_USER` and `REINDEX_USER` are exempt: the former targets an entity
  that does not exist yet, the latter is index-only.) Opting out via
  `--no-backup` requires an interactive typed confirmation (`no-backup`) that
  `--yes` does not bypass.
- **Instance fingerprint guard.** `apply` refuses to run against a GMS whose URL
  fingerprint does not match the one recorded in the plan, unless `--force` is
  passed.
- **Interactive confirmations.** `apply` prompts before making changes; a
  cleanup-phase plan with hard delete additionally requires a second typed
  confirmation, since hard deletion is irreversible.
- **Idempotent, resumable apply.** Change state is tracked per change in
  `plan.json`; re-running `apply` skips `DONE` changes and retries
  `PENDING`/`FAILED` ones, so an interrupted run can be resumed safely.
- **Audit trail.** `plan.json` records what was planned and, after `apply`, what
  actually happened per change (state and error message on failure).

## Testing

- **Unit suite** — 88 tests covering plan building, mapping resolution, and the
  plan store; ownership, policy, and view transforms; subscription
  deduplication; resume semantics; the dry-run and backup guarantees; and
  discovery-failure paths. Measured line coverage for the unit suite (excluding
  the end-to-end tests) is 65%:

  ```bash
  pip install -e ".[dev]"
  python -m pytest tests -q --ignore=tests/e2e --cov=dhusermig --cov-report=term
  ```

- **Golden end-to-end suite** — runs in CI against a real DataHub quickstart
  instance: seed a known fixture, run the full migrate plan/apply/verify and
  cleanup plan/apply/verify sequence, and compare the resulting plans and state
  against golden files (`tests/e2e/`, `.github/workflows/e2e.yml`).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).

This tool is provided as-is under the Apache-2.0 license. It is not part of the
managed DataHub Cloud service. Always review a plan (`plan.json` and
`summary.txt`) before applying it to a production instance.
