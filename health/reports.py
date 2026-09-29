"""Сборка выгружаемого Markdown-отчёта по репозиторию."""

from django.template.loader import render_to_string

from health.models import HealthScore, MetricSample, Repository, Scan
from health.scan_display import scan_for_card
from health.scoring import (
    CATEGORY_LABELS,
    INSUFFICIENT_SCORE_DETAIL,
    INSUFFICIENT_SCORE_TITLE,
    is_preliminary_score,
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

    Исходные баллы категории лежат в scan.raw. Здесь они снова
    переводятся в пункты итога, без записи в базу.
    """

    findings = list(scan.findings.all())
    raw = scan.raw or {}
    saved = raw.get("finding_category_points")
    if isinstance(saved, dict):
        pairs = [
            (
                item.category,
                int(saved.get(str(item.id), item.estimated_score_impact) or 0),
            )
            for item in findings
        ]
    else:
        pairs = [
            (item.category, item.estimated_score_impact) for item in findings
        ]

    scores = {row.category: row for row in scan.scores.all()}
    stored = raw.get("health_score")
    impacts = overall_finding_impacts(
        pairs,
        {category: row.total for category, row in scores.items()},
        {category: row.weight_used or 0.0 for category, row in scores.items()},
        overall_score=stored if isinstance(stored, int) else None,
    )
    for item, impact in zip(findings, impacts):
        item.estimated_score_impact = impact
    findings.sort(
        key=lambda item: (-item.estimated_score_impact, -int(item.severity or 0))
    )
    return findings


def build_markdown_report(repository: Repository) -> str:
    """Markdown по скану, который виден на карточке."""

    scan, scan_notice = scan_for_card(repository)
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
    presented = present_scores(totals)
    overall = presented["total"]
    is_preliminary = presented["is_preliminary"]
    if scan is not None:
        stored = (scan.raw or {}).get("health_score")
        raw_flag = (scan.raw or {}).get("is_preliminary")
        if isinstance(raw_flag, bool):
            is_preliminary = raw_flag and (
                isinstance(stored, int) or presented.get("computed_total") is not None
            )
        if isinstance(stored, int) and not is_preliminary:
            overall = stored
        elif is_preliminary:
            overall = None
    if overall is None and scan is not None and not is_preliminary:
        overall = overall_from_category_totals(totals)
        if overall is not None and is_preliminary_score(totals):
            overall = None
            is_preliminary = True

    return render_to_string(
        "health/report.md",
        {
            "repo": repository,
            "scan": scan,
            "overall": overall,
            "is_preliminary": is_preliminary,
            "insufficient_score_title": INSUFFICIENT_SCORE_TITLE,
            "insufficient_score_detail": INSUFFICIENT_SCORE_DETAIL,
            "scan_notice": scan_notice,
            "categories": categories,
            "findings": findings,
            "has_null": any(row["total"] is None for row in categories),
            "analyzed_at": scan.finished_at if scan else None,
        },
    )
