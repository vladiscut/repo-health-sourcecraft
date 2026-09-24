"""Прогон анализа категории CI/CD для одного Scan.

Категория считается только в личном контуре: токен берётся из
Profile.sourcecraft_pat пользователя Scan.triggered_by_user.
Без токена пишется HealthScore с total=None и запросов к CI нет.

Модуль не меняет Scan.status и не считает итоговый Repo Health Score.
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.db import transaction

from core.utils import parse_datetime
from health.models import Finding, HealthScore, MetricSample, Profile, Scan
from health.scoring import CATEGORY_WEIGHTS, scale, weighted_submetric_score
from integrations.sourcecraft import SourceCraftClient, SourceCraftError

logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.CI_CD
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

CI_CONFIG_PATH = ".sourcecraft/ci.yaml"
NO_CONFIG_SCORE = 15
CONFIG_ONLY_SCORE = 40

SUCCESS_RATE_WEIGHT = 0.75
DURATION_WEIGHT = 0.25
SUBMETRIC_WEIGHTS = {
    "success_rate": SUCCESS_RATE_WEIGHT,
    "duration": DURATION_WEIGHT,
}

DURATION_WORST_MINUTES = 60.0
DURATION_BEST_MINUTES = 5.0
SLOW_RUN_MINUTES = 30.0
LOW_SUCCESS_RATE = 0.8

SUCCESS_STATUS = "success"
FAILURE_STATUSES = frozenset({"failed", "timeout"})


@dataclass
class _RunStats:
    success_count: int = 0
    failure_count: int = 0
    success_rate: float | None = None
    median_duration_minutes: float | None = None
    last_red_url: str = ""
    durations: list[float] = field(default_factory=list)


def _user_pat(scan: Scan) -> str:
    user = scan.triggered_by_user
    if user is None:
        return ""
    try:
        profile = user.profile
    except Profile.DoesNotExist:
        return ""
    return (profile.sourcecraft_pat or "").strip()


def _save_unavailable(scan: Scan, reason: str) -> HealthScore:
    with transaction.atomic():
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key="_category_unavailable",
            defaults=dict(
                value=None,
                is_available=False,
                error_reason=reason[:255],
            ),
        )
        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=None,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=0.0,
                raw_metrics={"reason": reason},
            ),
        )
    return health_score


def _save_metric(
    scan: Scan,
    key: str,
    value: Any,
    unit: str = "",
    is_available: bool = True,
    reason: str = "",
    source_reference: str = "",
) -> None:
    MetricSample.objects.update_or_create(
        scan=scan,
        category=CATEGORY,
        metric_key=key,
        defaults=dict(
            value=value,
            unit=unit,
            is_available=is_available,
            error_reason=reason,
            source_reference=source_reference,
        ),
    )


def _config_in_tree(tree: list[dict[str, Any]]) -> bool:
    target = CI_CONFIG_PATH.lower()
    for entry in tree:
        path = str(entry.get("path") or entry.get("name") or "").lower()
        if path == target:
            return True
    return False


def _run_url(org_slug: str, repo_slug: str, slug: str) -> str:
    base = settings.SOURCECRAFT_API_BASE_URL.rstrip("/")
    return (
        f"{base}/repos/"
        f"{quote(org_slug, safe='')}/"
        f"{quote(repo_slug, safe='')}/"
        f"cicd/runs/{quote(slug, safe='')}"
    )


def _run_dates(run: dict[str, Any]) -> dict[str, Any]:
    dates = run.get("dates")
    return dates if isinstance(dates, dict) else {}


def _finished_at(run: dict[str, Any]) -> datetime | None:
    return parse_datetime(_run_dates(run).get("finished_at"))


def _duration_minutes(run: dict[str, Any]) -> float | None:
    dates = _run_dates(run)
    started = parse_datetime(dates.get("started_at"))
    finished = parse_datetime(dates.get("finished_at"))
    if started is None or finished is None or finished < started:
        return None
    return (finished - started).total_seconds() / 60.0


def _collect_runs(runs: list[dict[str, Any]], org_slug: str, repo_slug: str) -> _RunStats:
    stats = _RunStats()
    last_red_at: datetime | None = None

    for run in runs:
        status = str(run.get("status") or "").lower()
        if status == SUCCESS_STATUS:
            stats.success_count += 1
        elif status in FAILURE_STATUSES:
            stats.failure_count += 1
            finished = _finished_at(run)
            slug = str(run.get("slug") or run.get("id") or "")
            if slug and (last_red_at is None or (finished and finished >= last_red_at)):
                last_red_at = finished or last_red_at
                stats.last_red_url = _run_url(org_slug, repo_slug, slug)

        duration = _duration_minutes(run)
        if duration is not None and status in {SUCCESS_STATUS, *FAILURE_STATUSES}:
            stats.durations.append(duration)

    finished = stats.success_count + stats.failure_count
    if finished:
        stats.success_rate = stats.success_count / finished
    if stats.durations:
        stats.median_duration_minutes = statistics.median(stats.durations)
    return stats


def _score(stats: _RunStats) -> tuple[int, float, dict[str, float]]:
    submetric_scores: dict[str, float] = {}
    if stats.success_rate is not None:
        submetric_scores["success_rate"] = scale(stats.success_rate, worst=0.0, best=1.0)
    if stats.median_duration_minutes is not None:
        submetric_scores["duration"] = scale(
            stats.median_duration_minutes,
            worst=DURATION_WORST_MINUTES,
            best=DURATION_BEST_MINUTES,
        )
    if not submetric_scores:
        return CONFIG_ONLY_SCORE, 0.0, submetric_scores
    total, completeness = weighted_submetric_score(submetric_scores, SUBMETRIC_WEIGHTS)
    return total if total is not None else CONFIG_ONLY_SCORE, completeness, submetric_scores


def _build_findings(
    scan: Scan,
    config_present: bool,
    stats: _RunStats | None,
    category_score: int | None,
) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()
    findings: list[Finding] = []

    if not config_present:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.HIGH,
                title="Нет конфигурации SourceCraft CI",
                detail="В дереве репозитория не найден .sourcecraft/ci.yaml.",
                recommendation="Добавьте .sourcecraft/ci.yaml с базовой проверкой сборки.",
                evidence_refs=[CI_CONFIG_PATH],
                estimated_score_impact=10,
            )
        )
    elif stats is not None and stats.success_rate is not None and stats.success_rate < LOW_SUCCESS_RATE:
        refs = [stats.last_red_url] if stats.last_red_url else []
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.HIGH,
                title="Много неуспешных прогонов CI",
                detail=(
                    f"Доля успешных завершённых прогонов — {stats.success_rate:.0%}. "
                    f"Падений: {stats.failure_count}."
                ),
                recommendation="Разберите последний красный прогон и почините падающую проверку.",
                evidence_refs=refs,
                estimated_score_impact=15,
            )
        )

    if (
        stats is not None
        and stats.median_duration_minutes is not None
        and stats.median_duration_minutes > SLOW_RUN_MINUTES
    ):
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.MEDIUM,
                title="Долгие прогоны CI",
                detail=(
                    f"Медианная длительность завершённого прогона — "
                    f"{stats.median_duration_minutes:.0f} мин."
                ),
                recommendation="Сократите пайплайн: кэш зависимостей и меньше шагов на каждый push.",
                evidence_refs=[],
                estimated_score_impact=5,
            )
        )

    if findings and category_score is not None:
        Finding.objects.bulk_create(findings)


def _save_scored(
    scan: Scan,
    *,
    total: int,
    data_completeness: float,
    config_present: bool,
    stats: _RunStats | None,
    submetric_scores: dict[str, float],
    runs_reason: str = "",
) -> HealthScore:
    runs_available = stats is not None and stats.success_rate is not None
    duration_available = stats is not None and stats.median_duration_minutes is not None
    with transaction.atomic():
        _save_metric(
            scan,
            "ci_config_present",
            config_present,
            source_reference=CI_CONFIG_PATH if config_present else "",
        )
        _save_metric(
            scan,
            "ci_success_rate",
            None if stats is None else stats.success_rate,
            unit="ratio",
            is_available=runs_available,
            reason="" if runs_available else runs_reason,
            source_reference="" if stats is None else stats.last_red_url,
        )
        _save_metric(
            scan,
            "ci_median_duration_minutes",
            None if stats is None else stats.median_duration_minutes,
            unit="minutes",
            is_available=duration_available,
            reason="" if duration_available else runs_reason,
        )
        _build_findings(scan, config_present, stats, total)
        raw = {
            "ci_config_present": config_present,
            "submetric_scores": submetric_scores,
        }
        if stats is not None:
            raw.update(
                success_count=stats.success_count,
                failure_count=stats.failure_count,
                success_rate=stats.success_rate,
                median_duration_minutes=stats.median_duration_minutes,
                last_red_url=stats.last_red_url,
            )
        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=total,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=data_completeness,
                raw_metrics=raw,
            ),
        )
    return health_score


def run_cicd_scan(scan: Scan, client: SourceCraftClient) -> HealthScore:
    repository = scan.repository
    try:
        tree = client.get_repository_file_tree(
            repository.sourcecraft_id,
            repository.default_branch or None,
        )
    except SourceCraftError as exc:
        logger.error(f"Не удалось получить дерево файлов {repository}: {exc}")
        return _save_unavailable(scan, str(exc))

    if not _config_in_tree(tree):
        return _save_scored(
            scan,
            total=NO_CONFIG_SCORE,
            data_completeness=1.0,
            config_present=False,
            stats=None,
            submetric_scores={},
            runs_reason="нет .sourcecraft/ci.yaml",
        )

    try:
        runs = client.get_ci_pipelines(repository.sourcecraft_id)
    except SourceCraftError as exc:
        if exc.status_code == 404:
            logger.info(f"Список CI-прогонов недоступен для {repository}: {exc}")
            return _save_scored(
                scan,
                total=CONFIG_ONLY_SCORE,
                data_completeness=0.0,
                config_present=True,
                stats=None,
                submetric_scores={},
                runs_reason="список прогонов не найден",
            )
        logger.error(f"Не удалось получить CI-прогоны {repository}: {exc}")
        return _save_unavailable(scan, str(exc))

    stats = _collect_runs(runs, repository.org_slug, repository.repo_slug)
    total, completeness, submetric_scores = _score(stats)
    runs_reason = "" if stats.success_rate is not None else "нет завершённых прогонов"
    return _save_scored(
        scan,
        total=total,
        data_completeness=completeness,
        config_present=True,
        stats=stats,
        submetric_scores=submetric_scores,
        runs_reason=runs_reason,
    )


def run(scan_id: int) -> int:
    scan = Scan.objects.select_related("repository", "triggered_by_user").get(pk=scan_id)
    pat = _user_pat(scan)
    if not pat:
        health_score = _save_unavailable(
            scan,
            "нет персонального токена SourceCraft у владельца скана",
        )
        return health_score.pk

    client = SourceCraftClient(token=pat)
    health_score = run_cicd_scan(scan, client)
    return health_score.pk
