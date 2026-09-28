"""Сборка выгружаемого Markdown-отчёта по репозиторию."""

from django.template.loader import render_to_string

from health.models import HealthScore, MetricSample, Repository, Scan
from health.scoring import (
    CATEGORY_LABELS,
    overall_finding_impacts,
    overall_from_category_totals,
    present_scores,
)
from health.security_scan import plain_security_reason


SEVERITY_ORDER = {
    "4": 0,
    "3": 1,
    "2": 2,
    "1": 3,
}


def _category_reason(score: HealthScore | None) -> str:
    if score is None:
        return "категория не рассчитана"
    if score.total is not None:
        return ""
    raw = score.raw_metrics or {}
    reason = raw.get("reason") or raw.get("error") or ""
    if reason:
        return plain_security_reason(str(reason))
    sample = (
        MetricSample.objects.filter(
            scan_id=score.scan_id,
            category=score.category,
            is_available=False,
        )
        .exclude(error_reason="")
        .order_by("id")
        .first()
    )
    if sample and sample.error_reason:
        return plain_security_reason(sample.error_reason)
    return "нет данных"


def _categories_for_report(scan: Scan | None) -> list[dict]:
    by_category = {}
    if scan is not None:
        by_category = {row.category: row for row in scan.scores.all()}

    rows = []
    for key, label in CATEGORY_LABELS.items():
        score = by_category.get(key)
        total = score.total if score is not None else None
        rows.append(
            {
                "key": key,
                "label": label,
                "total": total,
                "reason": _category_reason(score) if total is None else "",
            }
        )
    return rows


def with_overall_impacts(scan: Scan) -> list:
    """Находки с estimated_score_impact в баллах общего Score.

    После агрегации поле уже пересчитано, исходные баллы категории
    лежат в scan.raw. У более ранних сканов в поле ещё баллы категории:
    их нормируем здесь, не записывая обратно.
    """

    findings = list(scan.findings.all())
    raw = scan.raw or {}
    if "finding_category_points" in raw:
        return findings

    scores = {row.category: row for row in scan.scores.all()}
    impacts = overall_finding_impacts(
        [(item.category, item.estimated_score_impact) for item in findings],
        {category: row.total for category, row in scores.items()},
        {category: row.weight_used or 0.0 for category, row in scores.items()},
    )
    for item, impact in zip(findings, impacts):
        item.estimated_score_impact = impact
    findings.sort(
        key=lambda item: (-item.estimated_score_impact, -int(item.severity or 0))
    )
    return findings


def build_markdown_report(repository: Repository) -> str:
    """Markdown по последнему завершённому Scan репозитория."""

    scan = repository.latest_completed_scan()
    totals = {key: None for key in CATEGORY_LABELS}
    findings = []
    if scan is not None:
        totals.update({row.category: row.total for row in scan.scores.all()})
        findings = with_overall_impacts(scan)
        findings.sort(
            key=lambda item: (
                SEVERITY_ORDER.get(item.severity, 9),
                -item.estimated_score_impact,
                item.title,
            )
        )

    categories = _categories_for_report(scan)
    overall = present_scores(totals)["total"]
    if overall is None and scan is not None:
        overall = overall_from_category_totals(totals)

    return render_to_string(
        "health/report.md",
        {
            "repo": repository,
            "scan": scan,
            "overall": overall,
            "categories": categories,
            "findings": findings,
            "has_null": any(row["total"] is None for row in categories),
            "analyzed_at": scan.finished_at if scan else None,
        },
    )
