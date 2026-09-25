"""Прогон анализа категории "Activity" для одного Scan.

Модуль сфокусирован только на категории Activity — он не трогает
Scan.status итогового скана и не пересчитывает общий Repo Health Score.

Он отвечает за:

1. Получение merge requests, contributors и releases через SourceCraftClient.
2. Получение истории коммитов дефолтной ветки за ACTIVITY_LOOKBACK_DAYS
   через SourceCraftGitClient (git clone --shallow-since) и расчёт
   количества/частоты коммитов.
3. Определение последней известной активности по датам из всех
   доступных источников (Repository.last_updated, MR, releases, коммиты).
4. Получение сырых Activity-метрик и запись их в MetricSample.
5. Расчёт балла категории 0-100 (или None при отсутствии данных) —
   делегированный health.scoring.score_activity_category.
6. Формирование Finding по обнаруженным проблемам.

Важно:
- likes/hearts/diamonds из Repository не используются;
- сетевые вызовы (в т.ч. git clone) выполняются вне открытой DB-транзакции;
- эта категория целиком роутится на отдельную очередь `analysis.git`
  (см. core/celery.py) с воркером на prefork-пуле, потому что git clone —
  блокирующая subprocess-операция, которая на gevent-пуле держит greenlet
  и не даёт освободить слот другим задачам этого воркера. Из-за этого
  MR/contributors/releases для Activity тоже выполняются на этом воркере,
  а не на быстром gevent-пуле analysis.scheduled — это сознательный
  компромисс ради изоляции git-операций, а не побочный эффект.
- собственная арифметика score находится в health.scoring.
"""

import logging

from dataclasses import dataclass, field
from datetime import datetime
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.utils import parse_datetime
from health.models import Finding, HealthScore, MetricSample, Scan, Repository, Profile
from health.scoring import (
    ACTIVITY_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD,
    ACTIVITY_LOOKBACK_DAYS,
    ACTIVITY_RECENT_ACTIVITY_STALE_DAYS,
    bump_severity,
    CATEGORY_WEIGHTS,
    score_activity_category,
)
from integrations.sourcecraft import SourceCraftClient, SourceCraftError
from integrations.git import SourceCraftGitClient


logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.ACTIVITY

# Номинальный вес категории по ТЗ — единственный источник: health.scoring.
# Финальная перенормировка между всеми 6 категориями — задача
# health.orchestrator.aggregate_scan.
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]


@dataclass
class _ActivityMetrics:
    """Промежуточный результат вычислений — перед сохранением в БД."""

    # None = коммиты не удалось получить (ошибка git-клиента), а не 0.
    commits_30d: int | None = None
    commit_frequency_week: float | None = None
    commits_fetch_error: str = ""

    last_activity_at: datetime | None = None
    last_activity_age_days: float | None = None
    last_activity_source: str = ""

    contributors_count: int | None = None

    merge_requests_total: int | None = None
    merge_requests_30d: int | None = None

    releases_total: int | None = None
    releases_30d: int | None = None

    fetch_errors: dict[str, str] = field(default_factory=dict)


def _extract_dates(item: dict[str, Any], keys: tuple[str, ...]) -> list[datetime]:
    dates: list[datetime] = []
    for key in keys:
        parsed = parse_datetime(item.get(key))
        if parsed is not None:
            dates.append(parsed)
    return dates


def _latest_item_date(items: list[dict[str, Any]], keys: tuple[str, ...]) -> datetime | None:
    latest: datetime | None = None
    for item in items:
        dates = _extract_dates(item, keys)
        if not dates:
            continue
        candidate = max(dates)
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _count_recent(
    items: list[dict[str, Any]],
    now: datetime,
    keys: tuple[str, ...],
    lookback_days: int,
) -> int:
    threshold = now - timedelta(days=lookback_days)
    count = 0
    for item in items:
        dates = _extract_dates(item, keys)
        if any(value >= threshold for value in dates):
            count += 1
    return count


def _compute_commit_metrics(
    git_client: SourceCraftGitClient,
    repository: Repository,
) -> tuple[int | None, float | None, str, list[datetime]]:
    """Возвращает (commits_30d, commit_frequency_week, error, commit_dates).

    Вызывается только при include_commits=True
    """

    try:
        commit_dates = git_client.get_commit_history(
            org_slug=repository.org_slug,
            repo_slug=repository.repo_slug,
            branch=repository.default_branch,
        )
    except SourceCraftError as exc:
        logger.warning(
            f"Не удалось получить историю коммитов для {repository}: {exc}"
        )
        return None, None, str(exc), []

    commits_30d = len(commit_dates)
    commit_frequency_week = commits_30d / (ACTIVITY_LOOKBACK_DAYS / 7.0)
    return commits_30d, commit_frequency_week, "", commit_dates


def _compute_metrics(
    client: SourceCraftClient,
    git_client: SourceCraftGitClient | None,
    repository: Repository,
    now: datetime,
    include_commits: bool,
) -> _ActivityMetrics:
    """Собирает сырые метрики Activity; API- и (опционально) git-клиенты
    вызываются здесь.

    `include_commits=False` (массовый плановый скан) — git_client вообще
    не дёргается: ни запроса, ни попытки клонирования не происходит,
    только явная пометка "сознательно не считалось".
    """

    repo_id = repository.sourcecraft_id
    metrics = _ActivityMetrics()

    last_activity_candidates: list[tuple[str, datetime]] = []

    if repository.last_updated is not None:
        last_activity_candidates.append(
            ("repository.last_updated", repository.last_updated)
        )

    try:
        merge_requests = client.get_merge_requests(repo_id)
    except SourceCraftError as exc:
        logger.warning(f"Не удалось получить merge requests для {repository}: {exc}")
        merge_requests = None
        metrics.fetch_errors["merge_requests"] = str(exc)

    try:
        contributors = client.get_contributors(repo_id)
    except SourceCraftError as exc:
        logger.warning(f"Не удалось получить contributors для {repository}: {exc}")
        contributors = None
        metrics.fetch_errors["contributors"] = str(exc)

    try:
        releases = client.get_releases(repo_id)
    except SourceCraftError as exc:
        logger.warning(f"Не удалось получить releases для {repository}: {exc}")
        releases = None
        metrics.fetch_errors["releases"] = str(exc)

    if merge_requests is not None:
        metrics.merge_requests_total = len(merge_requests)
        metrics.merge_requests_30d = _count_recent(
            merge_requests, now,
            keys=("created_at", "updated_at", "merged_at", "closed_at"),
            lookback_days=ACTIVITY_LOOKBACK_DAYS,
        )
        mr_last_activity = _latest_item_date(
            merge_requests, keys=("updated_at", "merged_at", "closed_at", "created_at"),
        )
        if mr_last_activity is not None:
            last_activity_candidates.append(("merge_requests", mr_last_activity))

    if contributors is not None:
        metrics.contributors_count = len(contributors)

    if releases is not None:
        metrics.releases_total = len(releases)
        metrics.releases_30d = _count_recent(
            releases, now,
            keys=("published_at", "released_at", "created_at", "updated_at"),
            lookback_days=ACTIVITY_LOOKBACK_DAYS,
        )
        releases_last_activity = _latest_item_date(
            releases, keys=("published_at", "released_at", "updated_at", "created_at"),
        )
        if releases_last_activity is not None:
            last_activity_candidates.append(("releases", releases_last_activity))

    if include_commits:
        commits_30d, commit_frequency_week, commits_error, commit_dates = (
            _compute_commit_metrics(git_client, repository)
        )
        metrics.commits_30d = commits_30d
        metrics.commit_frequency_week = commit_frequency_week
        metrics.commits_fetch_error = commits_error
        if commits_error:
            metrics.fetch_errors["commits"] = commits_error
        if commit_dates:
            last_activity_candidates.append(("commits", max(commit_dates)))
    else:
        # Не считаем: массовый плановый скан.
        # получения данных — commits в этом режиме не входят в формулу категории
        metrics.commits_fetch_error = "commits не считаются при массовом плановом скане"

    if last_activity_candidates:
        source, latest = max(last_activity_candidates, key=lambda pair: pair[1])
        metrics.last_activity_source = source
        metrics.last_activity_at = latest
        metrics.last_activity_age_days = max(0.0, (now - latest).total_seconds() / 86400.0)

    return metrics


def _save_metric_sample(
    scan: Scan,
    metric_key: str,
    value: Any,
    unit: str = "",
    *,
    is_available: bool = True,
    error_reason: str = "",
) -> None:
    MetricSample.objects.update_or_create(
        scan=scan,
        category=CATEGORY,
        metric_key=metric_key,
        defaults={
            "value": value,
            "unit": unit,
            "is_available": is_available,
            "error_reason": error_reason[:255],
        },
    )


def _save_metric_samples(scan: Scan, metrics: _ActivityMetrics) -> None:
    _save_metric_sample(
        scan, "activity_commits_30d", metrics.commits_30d, "commits",
        is_available=metrics.commits_30d is not None,
        error_reason=metrics.commits_fetch_error,
    )
    _save_metric_sample(
        scan, "activity_commit_frequency_week", metrics.commit_frequency_week, "commits/week",
        is_available=metrics.commit_frequency_week is not None,
        error_reason=metrics.commits_fetch_error,
    )

    _save_metric_sample(
        scan, "activity_last_activity_at",
        metrics.last_activity_at.isoformat() if metrics.last_activity_at is not None else None,
        "datetime",
        is_available=metrics.last_activity_at is not None,
        error_reason="" if metrics.last_activity_at is not None else "не удалось определить дату последней активности",
    )
    _save_metric_sample(
        scan, "activity_last_activity_age_days", metrics.last_activity_age_days, "days",
        is_available=metrics.last_activity_age_days is not None,
        error_reason="" if metrics.last_activity_age_days is not None else "не удалось определить возраст последней активности",
    )
    _save_metric_sample(
        scan, "activity_last_activity_source", metrics.last_activity_source, "source",
        is_available=metrics.last_activity_at is not None,
        error_reason="" if metrics.last_activity_at is not None else "источник последней активности недоступен",
    )

    _save_metric_sample(
        scan, "activity_contributors_count", metrics.contributors_count, "contributors",
        is_available=metrics.contributors_count is not None,
        error_reason="" if metrics.contributors_count is not None else metrics.fetch_errors.get("contributors", "contributors недоступны"),
    )

    _save_metric_sample(
        scan, "activity_merge_requests_total", metrics.merge_requests_total, "merge requests",
        is_available=metrics.merge_requests_total is not None,
        error_reason="" if metrics.merge_requests_total is not None else metrics.fetch_errors.get("merge_requests", "merge requests недоступны"),
    )
    _save_metric_sample(
        scan, "activity_merge_requests_30d", metrics.merge_requests_30d, "merge requests",
        is_available=metrics.merge_requests_30d is not None,
        error_reason="" if metrics.merge_requests_30d is not None else metrics.fetch_errors.get("merge_requests", "merge requests недоступны"),
    )

    _save_metric_sample(
        scan, "activity_releases_total", metrics.releases_total, "releases",
        is_available=metrics.releases_total is not None,
        error_reason="" if metrics.releases_total is not None else metrics.fetch_errors.get("releases", "releases недоступны"),
    )
    _save_metric_sample(
        scan, "activity_releases_30d", metrics.releases_30d, "releases",
        is_available=metrics.releases_30d is not None,
        error_reason="" if metrics.releases_30d is not None else metrics.fetch_errors.get("releases", "releases недоступны"),
    )

    for endpoint, reason in metrics.fetch_errors.items():
        _save_metric_sample(
            scan, f"activity_{endpoint}_fetch_error", None,
            is_available=False, error_reason=reason,
        )


def _bump(severity: str, category_score: float | None) -> str:
    return bump_severity(
        severity,
        category_score,
        ACTIVITY_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD
    )


def _build_findings(
    scan: Scan,
    metrics: _ActivityMetrics,
    category_score: int | None,
    include_commits: bool,
) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()

    findings: list[Finding] = []

    if metrics.last_activity_age_days is not None and metrics.last_activity_age_days > ACTIVITY_RECENT_ACTIVITY_STALE_DAYS:
        age_days = int(metrics.last_activity_age_days)
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.HIGH, category_score),
            title="Проект давно не проявлял активность",
            detail=f"Последняя известная активность была около {age_days} дней назад (источник: {metrics.last_activity_source}).",
            recommendation="Проверьте актуальность проекта, backlog и владельцев активных направлений.",
            evidence_refs=[metrics.last_activity_source, metrics.last_activity_at.isoformat() if metrics.last_activity_at else ""],
            estimated_score_impact=8,
        ))

    if (
        metrics.commits_30d == 0
        and (metrics.last_activity_age_days is None or metrics.last_activity_age_days <= ACTIVITY_RECENT_ACTIVITY_STALE_DAYS)
    ):
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.MEDIUM, category_score),
            title="Нет коммитов в дефолтную ветку за последние 30 дней",
            detail="В истории коммитов дефолтной ветки за последние 30 дней не найдено ни одного коммита.",
            recommendation="Проверьте, ведётся ли разработка в этой ветке — возможно, изменения идут в другую ветку или через форки.",
            evidence_refs=["commits:30d"],
            estimated_score_impact=4,
        ))

    if (
        metrics.merge_requests_30d is not None and metrics.merge_requests_30d == 0
        and (metrics.last_activity_age_days is None or metrics.last_activity_age_days <= ACTIVITY_RECENT_ACTIVITY_STALE_DAYS)
    ):
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.MEDIUM, category_score),
            title="Нет merge requests за последние 30 дней",
            detail="За последние 30 дней не найдено merge requests, созданных, обновлённых, объединённых или закрытых.",
            recommendation="Проверьте, соответствует ли фактический процесс разработки ожидаемому review/workflow-процессу.",
            evidence_refs=["merge_requests:30d"],
            estimated_score_impact=4,
        ))

    if metrics.contributors_count is not None and metrics.contributors_count <= 1:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Очень узкая база contributors",
            detail=f"В SourceCraft найдено {metrics.contributors_count} contributor.",
            recommendation="Зафиксируйте процесс участия и при необходимости расширьте круг contributors, чтобы снизить зависимость от одного владельца.",
            evidence_refs=["contributors"],
            estimated_score_impact=2,
        ))

    if metrics.releases_total is not None and metrics.releases_total > 0 and metrics.releases_30d == 0:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Нет релиза за последние 30 дней",
            detail=f"Всего найдено {metrics.releases_total} релизов, но за последние 30 дней новых релизов не обнаружено.",
            recommendation="Проверьте график поставки и предсказуемость выпуска изменений, если проект предполагает регулярные релизы.",
            evidence_refs=["releases:30d"],
            estimated_score_impact=2,
        ))

    if metrics.commits_fetch_error and metrics.commits_30d is None and include_commits:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=Finding.Severity.LOW,
            title="Не удалось получить историю коммитов",
            detail=f"Ошибка при клонировании репозитория для подсчёта коммитов: {metrics.commits_fetch_error}",
            recommendation="Проверьте доступность git-протокола SourceCraft и корректность default_branch репозитория.",
            evidence_refs=[],
            estimated_score_impact=0,
        ))

    if not findings and category_score is None:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=Finding.Severity.LOW,
            title="Недостаточно данных для оценки категории Activity",
            detail="Не удалось получить ни одной метрики, которая участвует в расчёте Activity score.",
            recommendation="Проверьте доступность SourceCraft API и git-протокола, а также формат данных merge requests, contributors, releases.",
            evidence_refs=[],
            estimated_score_impact=0,
        ))

    if findings:
        Finding.objects.bulk_create(findings)


def run_activity_scan(
    scan: Scan,
    client: SourceCraftClient,
    git_client: SourceCraftGitClient | None,
    include_commits: bool,
) -> HealthScore:
    """Собирает данные по активности репозитория и сохраняет результат"""

    repository = scan.repository
    now = timezone.now()

    metrics = _compute_metrics(
        client=client,
        git_client=git_client,
        repository=repository,
        now=now,
        include_commits=include_commits,
    )

    score, data_completeness, submetric_scores = score_activity_category(
        metrics, include_commits=include_commits
    )

    with transaction.atomic():
        _save_metric_samples(scan, metrics)
        _build_findings(scan, metrics, score, include_commits=include_commits)

        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults={
                "total": score,
                "weight_used": CATEGORY_WEIGHT,
                "data_completeness": data_completeness,
                "raw_metrics": {
                    "include_commits": include_commits,
                    "commits_30d": metrics.commits_30d,
                    "commit_frequency_week": metrics.commit_frequency_week,
                    "last_activity_at": metrics.last_activity_at.isoformat() if metrics.last_activity_at is not None else None,
                    "last_activity_age_days": metrics.last_activity_age_days,
                    "last_activity_source": metrics.last_activity_source,
                    "contributors_count": metrics.contributors_count,
                    "merge_requests_total": metrics.merge_requests_total,
                    "merge_requests_30d": metrics.merge_requests_30d,
                    "releases_total": metrics.releases_total,
                    "releases_30d": metrics.releases_30d,
                    "fetch_errors": metrics.fetch_errors,
                    "submetric_scores": submetric_scores,
                },
            },
        )

    return health_score


def run(scan_id: int) -> int:
    scan = Scan.objects.select_related("repository").get(pk=scan_id)

    # Критерий — то, КАК запущен скан:
    # SCHEDULE/MANUAL — массовый плановый скан (потенциально тысячи
    # репозиториев разом), commits в нём не считаются;
    # USER — одиночный ручной запуск для одного репозитория,
    # commits считаются через git clone.
    include_commits = scan.triggered_by == Scan.TriggeredBy.USER

    client = SourceCraftClient()
    git_client = None
    if include_commits and scan.triggered_by_user_id:
        token = scan.triggered_by_user.profile.sourcecraft_token
        git_client = SourceCraftGitClient(token=token)

    health_score = run_activity_scan(scan, client, git_client, include_commits)
    return health_score.pk
