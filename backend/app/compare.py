"""Compare two stored reports check by check, using only their recorded statuses."""

from typing import Any

from app.schemas import CheckChange, RunComparison

GOOD = {"passed"}
BAD = {"failed", "error"}


def _classify(base: str | None, head: str | None) -> str:
    if base is None:
        return "added"
    if head is None:
        return "removed"
    if base == head:
        return "unchanged"
    if base in BAD and head in GOOD:
        return "fixed"
    if base not in BAD and head in BAD:
        return "regressed"
    return "changed"


def compare_reports(base: dict[str, Any], head: dict[str, Any]) -> RunComparison:
    base_checks = {check["check_id"]: check["status"] for check in base.get("checks", [])}
    head_checks = {check["check_id"]: check["status"] for check in head.get("checks", [])}
    order = list(base_checks) + [name for name in head_checks if name not in base_checks]
    changes = [
        CheckChange(
            check_id=name,
            base_status=base_checks.get(name),
            head_status=head_checks.get(name),
            change=_classify(base_checks.get(name), head_checks.get(name)),
        )
        for name in order
    ]
    return RunComparison(
        base_run_id=base["run_id"],
        head_run_id=head["run_id"],
        base_status=base["overall_status"],
        head_status=head["overall_status"],
        same_target=base.get("target_url") == head.get("target_url"),
        changes=changes,
        regressions=sum(change.change == "regressed" for change in changes),
        fixes=sum(change.change == "fixed" for change in changes),
    )
