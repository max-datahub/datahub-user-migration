# Runbook

## Before anything else: prevent the problem instead of migrating around it

This tool exists because a DataHub user URN is derived from the login email
(`urn:li:corpuser:<email>`). If the IdP's username/subject claim is the email
address, any domain rename, company rebrand, or email-format change mints a brand
new, disconnected user URN — and everything anchored to the old URN (ownership,
subscriptions, groups, policies, personal views, tokens) has to be re-pointed,
which is exactly what this tool automates.

**If you have any say over the IdP configuration, configure it to send a stable,
domain-independent username claim — a unique user ID, not the email address — as
the identifier DataHub uses for the corpuser URN.** Do this before the next domain
change, not after. The best migration is the one you never have to run.

Everything below assumes the email-derived URNs already exist and must be
migrated.

## Order of operations

A migration is two passes — **migrate**, then **cleanup** — each following the same
plan → review → apply → verify shape:

```
plan (migrate)  →  review plan.json/summary.txt  →  apply  →  verify
                                                                  │
                                        (confidence period; new user in production)
                                                                  │
plan --phase cleanup  →  review  →  apply  →  verify
```

Per-migration checklist:

1. `datahub-user-migration plan --user <old> --target-domain <new> --out out/migrate`
   Nothing is written to DataHub — this only reads state and produces
   `out/migrate/plan.json` and `out/migrate/summary.txt`. Planning is
   **fail-fast**: if any discovery step fails (ownership discovery with both its
   relationship-walk and search paths failing, or subscription, policy, token,
   view, homepage, or ingestion-source listing failing), plan building aborts
   with a `DiscoveryError` instead of producing a silently partial plan. Fix the
   connectivity or permission issue and re-run `plan`.
2. **Review both files.** `plan.json` is the full, structured change list — read
   it, diff it, have a second person review it. It is also the **resume/state
   file**: `apply` updates it in place (each change's state moves
   `PENDING → DONE`/`FAILED` as it goes), so it is the audit trail of what
   actually happened, not just what was intended. `summary.txt` is the
   human-readable version, with a "Manual follow-ups" section listing every
   detect-only finding (tokens, personal views, homepage state, recreation-risk
   ingestion sources) plus the recipe-fix snippet (see below).
   To preview the apply without any side effects, run
   `apply --dry-run`: it is fully read-only — no writes to DataHub, no backup
   files, and `plan.json` is not modified.
3. `datahub-user-migration apply --plan out/migrate/plan.json` — applies the
   migrate-phase changes. Each entity is backed up before its first mutation
   (see [Rollback / manual restore](#rollback--manual-restore)); if a backup
   cannot be taken, that change is marked `FAILED` and the entity is **not**
   mutated. `apply` also refuses to run against a GMS whose URL fingerprint does
   not match the one recorded in the plan, unless `--force` is passed (a
   guardrail against applying a plan built against a different instance).
4. `datahub-user-migration verify --plan out/migrate/plan.json` — confirms the
   new user now owns everything the migrate plan added. See
   [What `verify` checks](#what-verify-checks).
5. Let the new user operate for an appropriate confidence window, then build the
   cleanup plan:
   `datahub-user-migration plan --user <old> --target-domain <new> --phase cleanup --out out/cleanup`
6. Review, then `datahub-user-migration apply --plan out/cleanup/plan.json`.
7. `datahub-user-migration verify --plan out/cleanup/plan.json` — confirms the
   old user no longer owns the entities the cleanup plan removed it from.

`datahub-user-migration status --plan <plan.json>` works at any point in this
sequence to print per-kind, per-state change counts without touching DataHub.

## What `verify` checks

`verify` reads `plan.meta.phase` from the plan it is pointed at and runs a
different check per user depending on the phase. **Both checks are scoped to the
plan's own targets** — verification confirms the plan landed; it does not sweep
the whole instance for stray references.

- **Migrate plan**: collects the target of every `ADD_OWNERSHIP` change in the
  plan, then checks that the **new** user now owns each of them. `PASS` means
  the new user owns everything the migrate-phase `apply` intended to add. This
  check does not care whether the old owner is still present (it deliberately
  still is; removing it is cleanup's job). A `FAIL` lists which targets are
  still missing the new owner.
- **Cleanup plan**: collects the target of every `REMOVE_OWNERSHIP` change in
  the plan, then checks that the **old** user no longer owns any of them.
  `PASS` means the old owner is gone from every entity the cleanup plan covered.

Both checks fetch the Ownership aspect directly from primary storage for each of
the plan's targets, one target at a time — they never query the search or
relationship index, so a `verify` run immediately after `apply` is not subject
to index lag.

`verify` does **not** check subscriptions, policies, or the user clone — review
those via the plan states in `plan.json` (`status` summarizes them) and spot
checks in the UI.

## What is automated vs. manual

See the [Scope and limitations](../README.md#scope-and-limitations) section of
the README for the full breakdown. In short:

- **Automated**: ownership (all types, all ownership-bearing entity types),
  subscriptions, group and role membership (via the user clone), access-policy
  actors, and old-user removal + reindex on cleanup.
- **Detected and reported only**: personal access tokens, personal views,
  homepage personalization, and usage-based ingestion sources that can recreate
  the old user. These appear in the "Manual follow-ups" section of
  `summary.txt` and as INFO entries in `plan.json`.
- **Out of scope**: historical audit/timeline metadata (immutable by design),
  form assignments, incident assignees, and `createdBy`/actor stamps.

Note on policies: policies that grant access via `actors.resourceOwners` (a
"whoever owns the resource" boolean, not a named user) cannot be matched to a
specific user by aspect inspection; the tool logs how many such policies it saw
so they can be reviewed separately, but does not rewrite them.

### Preventing user recreation by usage-based ingestion

Ingestion sources that extract usage statistics (for example Snowflake or
BigQuery with a usage recipe block) can recreate a deleted or migrated corpuser
purely from query-history actors, undoing cleanup. The tool flags such sources
in the plan (`DETECT_RECREATION_SOURCE`); the fix must be applied to each recipe
manually. The exact guidance the tool emits (also included at the bottom of any
`summary.txt` with findings):

> To stop a usage-based ingestion source from recreating deleted/migrated users,
> either:
>   1. Disable usage extraction entirely: set `user_usage_enabled: false` in the
>      source's usage config block, OR
>   2. Scope usage extraction to known-good users: set a `user_email_pattern`
>      allow-list (e.g. `allow: ['^.*@newdomain\.com$']`) and/or
>      `pushdown_allow_usernames` to the migrated usernames only.

## Recovering from failures

### Interrupted apply

Re-run the exact same `apply` command. `apply` is idempotent and resumable:
changes already marked `DONE` in `plan.json` are skipped, and only
`PENDING`/`FAILED` changes are (re)attempted.

### A change is FAILED

1. Open `plan.json` and find the change — its `error` field records the failure
   message.
2. Fix the underlying cause (connectivity, permissions, a since-deleted target
   entity, and so on).
3. Re-run `apply` with the same plan; the `FAILED` change is retried.

A failure to take an entity's backup is treated the same way **by design**: the
change is marked `FAILED` and the entity is not mutated, because a mutation
without a pre-mutation backup would be unrecoverable. Re-running `apply` retries
the backup and then the mutation.

## Rollback / manual restore

Every `apply` backs up each entity — its full body as returned by the DataHub
OpenAPI v3 entity endpoint — **before** that entity's first mutation in the run.
Backups are on by default; skipping them (`--no-backup`) requires an interactive
typed confirmation and is not recommended.

There is **no automated restore command in this release.** Rolling back means
locating the backup file for the affected entity, extracting the relevant aspect,
and re-emitting it with the DataHub CLI, as follows.

**Backup file location and structure.** Backups are written to
`<plan dir>/backups/<sanitized-urn>.yaml` (or `.json` if PyYAML is not
installed), where `<sanitized-urn>` is the entity URN with every character other
than letters, digits, `-`, `_`, and `.` replaced by `_`. Each file has the
structure:

```yaml
_backup_meta:
  backed_up_at: "2026-07-23T10:00:00+00:00"
  entity_urn: "urn:li:dataset:(urn:li:dataPlatform:snowflake,my_db.my_schema.events,PROD)"
  entity_type: dataset
entity:
  urn: "urn:li:dataset:(urn:li:dataPlatform:snowflake,my_db.my_schema.events,PROD)"
  ownership:
    value:
      owners: [...]
      lastModified: {...}
  # ...one key per aspect, each with a nested `value` object
```

**To restore an aspect:**

1. Locate the backup file for the entity URN under `<plan dir>/backups/`.
2. Extract `entity.<aspectName>.value` (the aspect payload only — not the
   wrapper) into a standalone JSON file.
3. Re-emit it with `datahub put aspect` (the DataHub CLI installed alongside
   this tool; it reads the same `DATAHUB_GMS_URL`/`DATAHUB_GMS_TOKEN`
   environment variables):

```bash
datahub put aspect \
  --urn "<entity urn>" \
  --aspect <aspectName> \
  --aspect-data restore.json
```

**Worked example** — restoring the pre-migration `ownership` aspect of a dataset
from a YAML backup:

```bash
# 1. The backup file for
#    urn:li:dataset:(urn:li:dataPlatform:snowflake,my_db.my_schema.events,PROD)
#    is at (URN sanitized: every char outside [a-zA-Z0-9-_.] becomes _):
ls out/migrate/backups/urn_li_dataset__urn_li_dataPlatform_snowflake_my_db.my_schema.events_PROD_.yaml

# 2. Extract the ownership aspect payload into restore.json
python -c "
import json, yaml
doc = yaml.safe_load(open('out/migrate/backups/urn_li_dataset__urn_li_dataPlatform_snowflake_my_db.my_schema.events_PROD_.yaml'))
json.dump(doc['entity']['ownership']['value'], open('restore.json', 'w'), indent=2)
"

# 3. Re-emit it
datahub put aspect \
  --urn "urn:li:dataset:(urn:li:dataPlatform:snowflake,my_db.my_schema.events,PROD)" \
  --aspect ownership \
  --aspect-data restore.json
# -> Update succeeded with status 200
```

For a `.json` backup (PyYAML not installed), step 2 is the same extraction with
`json.load` instead of `yaml.safe_load`.
