from __future__ import annotations

from collections import Counter
from pathlib import Path

from dhusermig.apply.prevention import recipe_fix_snippet
from dhusermig.plan.schema import Plan, State


def write_summary(plan: Plan, path: Path) -> None:
    """Write a human-readable summary: per-user + per-kind change counts, plus a
    Manual follow-ups section listing every INFO finding and the recreation-source
    prevention snippet."""
    lines: list[str] = [
        f"# User migration plan summary ({plan.meta.phase})",
        "",
        f"Target domain: {plan.meta.target_domain}",
        f"Created at: {plan.meta.created_at}",
        f"GMS: {plan.meta.gms_url_fingerprint}",
        f"Users: {len(plan.users)}",
        "",
        "## Per-user changes",
    ]

    kind_totals: Counter = Counter()
    info_findings: list[tuple[str, str, str, str]] = []

    for user in plan.users:
        lines.append(f"- {user.old_email} -> {user.new_email} ({len(user.changes)} change(s))")
        counts = Counter(c.kind.value for c in user.changes)
        for kind, count in sorted(counts.items()):
            lines.append(f"    {kind}: {count}")
        kind_totals.update(counts)
        for c in user.changes:
            if c.state == State.INFO:
                info_findings.append((user.old_email, c.kind.value, c.target, c.note or ""))

    lines += ["", "## Totals by kind"]
    for kind, count in sorted(kind_totals.items()):
        lines.append(f"- {kind}: {count}")

    lines += ["", "## Manual follow-ups"]
    if info_findings:
        for old_email, kind, target, note in info_findings:
            lines.append(f"- [{kind}] {old_email}: {target} — {note}")
        lines += ["", recipe_fix_snippet()]
    else:
        lines.append("None.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
