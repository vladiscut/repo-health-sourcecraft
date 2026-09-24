"""
Прогон анализа категории "Issues" для одного Scan.

Использование:

    scan = Scan.objects.create(
        repository=repo,
        status=Scan.Status.RUNNING,
        triggered_by=Scan.TriggeredBy.SCHEDULE,
    )
    run_issues_scan(scan, client)

Функция сфокусирована только на категории Issues — она не
трогает Scan.status итогового скана и не пересчитывает общий Repo Health
Score.

Она отвечает за:

1. Получение issues репозитория через SourceCraftClient.
2. Вычисление сырых метрик категории Issues и запись их в MetricSample.
3. Расчёт балла категории 0-100 (или None при отсутствии данных) —
   делегирован в health.scoring.score_issues_category, здесь модуль
   только собирает _IssuesMetrics и не занимается арифметикой скоринга.
4. Формирование приоритизированных Finding по обнаруженным проблемам.
"""

import logging
import statistics

from dataclasses import dataclass, field
from datetime import datetime
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.utils import parse_datetime
from health.models import Finding, HealthScore, MetricSample, Scan
from health.scoring import (
    CATEGORY_WEIGHTS,
    ISSUES_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD,
    ISSUES_FIRST_RESPONSE_HOURS_FOR_ZERO_SCORE,
    ISSUES_FIRST_RESPONSE_SAMPLE_SIZE,
    ISSUES_LOOKBACK_DAYS,
    ISSUES_STALE_DAYS_THRESHOLD,
    ISSUES_TIME_TO_CLOSE_DAYS_FOR_ZERO_SCORE,
    bump_severity,
    score_issues_category,
)
from integrations.sourcecraft import SourceCraftClient, SourceCraftError

logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.ISSUES

# Номинальный вес категории по ТЗ — единственный источник: health.scoring.
# Финальная перенормировка между всеми 6 категориями — задача
# health.orchestrator.aggregate_scan.
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

# Пороги/сэмплинг сбора данных (не скоринга) — тоже общие с health.scoring,
# чтобы "зависшим" при подсчёте stale_count считалось ровно то же самое,
# что потом штрафуется в score_issues_category.
STALE_DAYS_THRESHOLD = ISSUES_STALE_DAYS_THRESHOLD
LOOKBACK_DAYS = ISSUES_LOOKBACK_DAYS
FIRST_RESPONSE_SAMPLE_SIZE = ISSUES_FIRST_RESPONSE_SAMPLE_SIZE


def _is_closed(issue: dict[str, Any]) -> bool:
    state = str(issue.get("state") or issue.get("status") or "").lower()
    return state in ("closed", "resolved", "done")


@dataclass
class _IssuesMetrics:
    """Промежуточный результат вычислений — перед сохранением в БД"""

    total_count: int = 0
    open_count: int = 0
    closed_count: int = 0
    created_30d: int = 0
    closed_30d: int = 0
    close_rate_30d: float | None = None
    stale_count: int = 0
    stale_ratio: float | None = None
    median_time_to_close_days: float | None = None
    median_first_response_hours: float | None = None
    first_response_sample_size: int = 0
    first_response_available: bool = False
    stale_issue_refs: list[str] = field(default_factory=list)


def _compute_metrics(
    client: SourceCraftClient,
    repo_id: str,
    issues: list[dict[str, Any]],
    now: datetime,
) -> _IssuesMetrics:
    metrics = _IssuesMetrics()
    metrics.total_count = len(issues)

    lookback_start = now - timedelta(days=LOOKBACK_DAYS)
    stale_before = now - timedelta(days=STALE_DAYS_THRESHOLD)

    close_durations_days: list[float] = []

    for issue in issues:
        created_at = parse_datetime(issue.get("created_at"))
        updated_at = parse_datetime(issue.get("updated_at")) or created_at
        closed_at = parse_datetime(issue.get("closed_at"))

        is_closed = _is_closed(issue) or closed_at is not None

        if is_closed:
            metrics.closed_count += 1
            if closed_at and closed_at >= lookback_start:
                metrics.closed_30d += 1
            if created_at and closed_at:
                close_durations_days.append((closed_at - created_at).total_seconds() / 86400)
        else:
            metrics.open_count += 1
            if updated_at and updated_at < stale_before:
                metrics.stale_count += 1
                ref = str(issue.get("id") or issue.get("number") or issue.get("url") or "")
                if ref:
                    metrics.stale_issue_refs.append(ref)

        if created_at and created_at >= lookback_start:
            metrics.created_30d += 1

    if metrics.open_count > 0:
        metrics.stale_ratio = metrics.stale_count / metrics.open_count

    if metrics.created_30d > 0:
        metrics.close_rate_30d = min(1.0, metrics.closed_30d / metrics.created_30d)
    elif metrics.total_count == 0:
        metrics.close_rate_30d = None
    else:
        # Ничего не создавалось за период, но что-то закрывалось —
        # трактуем как хорошую динамику
        metrics.close_rate_30d = 1.0 if metrics.closed_30d > 0 else None

    if close_durations_days:
        metrics.median_time_to_close_days = statistics.median(close_durations_days)

    # Время до первого ответа: считаем по выборке последних issues, чтобы не
    # делать по запросу комментариев на каждый issue при большом трекере.
    sample = sorted(
        issues,
        key=lambda i: parse_datetime(i.get("created_at")) or now,
        reverse=True,
    )[:FIRST_RESPONSE_SAMPLE_SIZE]

    response_hours: list[float] = []
    for issue in sample:
        created_at = parse_datetime(issue.get("created_at"))
        if not created_at:
            continue
        issue_id = issue.get("id") or issue.get("number")
        if issue_id is None:
            continue
        try:
            comments = client.get_issue_events(repo_id, issue_id)
        except SourceCraftError as exc:
            logger.info(
                f"Не удалось получить комментарии issue {issue_id}: {exc}"
            )
            continue
        comment_dates = sorted(
            filter(
                None, (parse_datetime(c.get("created_at")) for c in comments)
            )
        )
        if comment_dates:
            first_comment = comment_dates[0]
            if first_comment >= created_at:
                response_hours.append((first_comment - created_at).total_seconds() / 3600)

    metrics.first_response_sample_size = len(sample)
    if response_hours:
        metrics.median_first_response_hours = statistics.median(response_hours)
        metrics.first_response_available = True

    return metrics


def _save_metric_samples(scan: Scan, metrics: _IssuesMetrics) -> None:
    def _save(key: str, value: Any, unit: str = "", is_available: bool = True, reason: str = "") -> None:
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key=key,
            defaults=dict(
                value=value,
                unit=unit,
                is_available=is_available,
                error_reason=reason,
            ),
        )

    _save("issues_total_count", metrics.total_count, "issues")
    _save("issues_open_count", metrics.open_count, "issues")
    _save("issues_closed_count", metrics.closed_count, "issues")
    _save("issues_created_30d", metrics.created_30d, "issues")
    _save("issues_closed_30d", metrics.closed_30d, "issues")
    _save(
        "issues_close_rate_30d",
        metrics.close_rate_30d,
        "ratio",
        is_available=metrics.close_rate_30d is not None,
        reason="" if metrics.close_rate_30d is not None else "нет issues за период",
    )
    _save("issues_stale_count", metrics.stale_count, "issues")
    _save(
        "issues_stale_ratio",
        metrics.stale_ratio,
        "ratio",
        is_available=metrics.stale_ratio is not None,
        reason="" if metrics.stale_ratio is not None else "нет открытых issues",
    )
    _save(
        "issues_median_time_to_close_days",
        metrics.median_time_to_close_days,
        "days",
        is_available=metrics.median_time_to_close_days is not None,
        reason="" if metrics.median_time_to_close_days is not None else "нет закрытых issues с датами",
    )
    _save(
        "issues_median_first_response_hours",
        metrics.median_first_response_hours,
        "hours",
        is_available=metrics.first_response_available,
        reason="" if metrics.first_response_available else "нет данных о комментариях в выборке",
    )


def _bump(severity: str, category_score: float | None) -> str:
    return bump_severity(
        severity, category_score, ISSUES_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD
    )


def _build_findings(scan: Scan, metrics: _IssuesMetrics, category_score: float | None) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()

    findings: list[Finding] = []

    if metrics.stale_ratio is not None and metrics.stale_count > 0:
        from health.scoring import ISSUES_STALE_RATIO_FOR_ZERO_SCORE

        severity = (
            Finding.Severity.HIGH
            if metrics.stale_ratio >= ISSUES_STALE_RATIO_FOR_ZERO_SCORE
            else Finding.Severity.MEDIUM
        )
        severity = _bump(severity, category_score)
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=severity,
                title=f"{metrics.stale_count} задач не обновлялись более {STALE_DAYS_THRESHOLD} дней",
                detail=(
                    f"Из {metrics.open_count} открытых issues {metrics.stale_count} "
                    f"({metrics.stale_ratio:.0%}) не получали обновлений более "
                    f"{STALE_DAYS_THRESHOLD} дней."
                ),
                recommendation=(
                    "Разберите зависшие issues: закройте неактуальные, "
                    "назначьте ответственных на остальные и настройте "
                    "автоматическую пометку stale через bot/CI."
                ),
                evidence_refs=metrics.stale_issue_refs[:20],
                estimated_score_impact=8 if severity == Finding.Severity.HIGH else 4,
            )
        )

    if metrics.first_response_available and metrics.median_first_response_hours is not None:
        if metrics.median_first_response_hours > ISSUES_FIRST_RESPONSE_HOURS_FOR_ZERO_SCORE / 2:
            findings.append(
                Finding(
                    scan=scan,
                    category=CATEGORY,
                    severity=_bump(Finding.Severity.MEDIUM, category_score),
                    title="Медленный первый ответ на issues",
                    detail=(
                        f"Медианное время до первого ответа на выборке из "
                        f"{metrics.first_response_sample_size} задач — "
                        f"{metrics.median_first_response_hours:.0f} ч."
                    ),
                    recommendation=(
                        "Настройте триаж новых issues (например, еженедельный "
                        "разбор или auto-label) и целевой SLA на первый ответ."
                    ),
                    evidence_refs=[],
                    estimated_score_impact=5,
                )
            )

    if metrics.median_time_to_close_days is not None and metrics.median_time_to_close_days > ISSUES_TIME_TO_CLOSE_DAYS_FOR_ZERO_SCORE / 2:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.MEDIUM, category_score),
                title="Долгое время закрытия задач",
                detail=f"Медианное время до закрытия issue — {metrics.median_time_to_close_days:.0f} дней.",
                recommendation="Приоритизируйте разбор бэклога и разбивайте крупные задачи на более мелкие с понятным критерием закрытия.",
                evidence_refs=[],
                estimated_score_impact=4,
            )
        )

    if metrics.total_count == 0:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.LOW,
                title="В репозитории не используется трекер issues",
                detail="Не найдено ни одной задачи — оценить категорию Issues невозможно.",
                recommendation="Заведите issues для известных багов и задач, чтобы отслеживать состояние проекта и дать пользователям канал для обратной связи.",
                evidence_refs=[],
                estimated_score_impact=0,
            )
        )

    if metrics.total_count > 0 and category_score is None:
        # total_count == 0 обрабатывается отдельным Finding выше ("нет
        # трекера issues") — это другой случай: issues есть, но ни одна
        # под-метрика не смогла посчитаться

        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.LOW,
                title="Недостаточно данных для оценки категории Issues",
                detail=(
                    f"Найдено {metrics.total_count} issues, но ни одна метрика "
                    "категории не смогла быть рассчитана (проблемы с датами "
                    "created_at/updated_at/closed_at в ответе API)."
                ),
                recommendation="Проверьте формат дат, отдаваемых SourceCraft API для issues этого репозитория.",
                evidence_refs=[],
                estimated_score_impact=0,
            )
        )

    if findings:
        Finding.objects.bulk_create(findings)


def run_issues_scan(scan: Scan, client: SourceCraftClient) -> HealthScore:
    """Собирает данные по issues репозитория и сохраняет результат категории"""

    repository = scan.repository
    repo_id = repository.sourcecraft_id
    now = timezone.now()

    try:
        issues = client.get_issues(repo_id)
    except SourceCraftError as exc:
        logger.error(f"Не удалось получить issues для {repository}: {exc}")
        with transaction.atomic():
            MetricSample.objects.update_or_create(
                scan=scan,
                category=CATEGORY,
                metric_key="issues_fetch_error",
                defaults=dict(
                    value=None,
                    is_available=False,
                    error_reason=str(exc),
                ),
            )
            health_score, _ = HealthScore.objects.update_or_create(
                scan=scan,
                category=CATEGORY,
                defaults=dict(
                    total=None,
                    weight_used=CATEGORY_WEIGHT,
                    data_completeness=0.0,
                    raw_metrics={},
                ),
            )
        return health_score

    metrics = _compute_metrics(client, repo_id, issues, now)
    score, data_completeness, submetric_scores = score_issues_category(metrics)

    with transaction.atomic():
        _save_metric_samples(scan, metrics)
        _build_findings(scan, metrics, score)

        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=score,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=data_completeness,
                raw_metrics={
                    "total_count": metrics.total_count,
                    "open_count": metrics.open_count,
                    "closed_count": metrics.closed_count,
                    "created_30d": metrics.created_30d,
                    "closed_30d": metrics.closed_30d,
                    "close_rate_30d": metrics.close_rate_30d,
                    "stale_count": metrics.stale_count,
                    "stale_ratio": metrics.stale_ratio,
                    "median_time_to_close_days": metrics.median_time_to_close_days,
                    "median_first_response_hours": metrics.median_first_response_hours,
                    "submetric_scores": submetric_scores,
                },
            ),
        )
    return health_score


def run(scan_id: int) -> int:
    client = SourceCraftClient()
    scan = Scan.objects.select_related("repository").get(pk=scan_id)
    health_score = run_issues_scan(scan, client)
    return health_score.pk
