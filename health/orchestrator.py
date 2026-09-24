"""
Оркестрация анализа репозитория.

Отвечает за:
  1. Создание одного Scan и параллельный запуск таск для категорий
  2. Агрегацию результатов всех категорий в итоговый Repo Health Score
  3. Периодический пересчёт публичных репозиториев (celery-beat)
"""
import datetime
import logging

from celery import chord, group

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.utils import timezone

from health.models import HealthScore, MetricSample, Repository, Scan
from health.scoring import CATEGORY_WEIGHTS
from health.tasks import (
    task_issues_scan,
    task_docs_scan,
    task_cicd_scan,
    task_security_scan,
    task_activity_scan,
    task_code_health_scan,
    task_check_and_scan_repository,
)
from integrations.sourcecraft import SourceCraftClient, SourceCraftError


logger = logging.getLogger(__name__)

# Веса категорий — единственный источник: health.scoring.CATEGORY_WEIGHTS.
ALL_CATEGORIES = list(CATEGORY_WEIGHTS.keys())

# Сколько Scan может провести в PENDING/RUNNING, прежде чем
# мы считаем его протухшим
SCAN_STALE_AFTER = datetime.timedelta(
    minutes=settings.SCAN_STALE_TIMEOUT_MINUTES
)


class ActiveScanExistsError(Exception):
    """По этому репозиторию уже есть незавершённый Scan (pending/running)"""


def _mark_stale_scans_as_failed(queryset) -> int:
    """
    Переводит в FAILED те Scan, что провисели в PENDING/RUNNING
    дольше SCAN_STALE_AFTER
    """

    threshold = timezone.now() - SCAN_STALE_AFTER
    stale_error = (
        f"Scan помечен как зависший: не завершался более {SCAN_STALE_AFTER}"
    )
    return queryset.filter(
        status__in=[Scan.Status.PENDING, Scan.Status.RUNNING],
        created_at__lt=threshold,
    ).update(
        status=Scan.Status.FAILED,
        finished_at=timezone.now(),
        error=stale_error,
    )


def _get_current_commit_hash(repository: Repository) -> str:
    """
    Запрашивает hash последнего коммита дефолтной ветки

    Если SourceCraft недоступен — это тоже "нет данных", а не повод
    считать репозиторий неизменившимся или блокировать скан: возвращаем
    пустую строку
    """

    try:
        client = SourceCraftClient()
        commit_hash = client.get_default_branch_hash(
            repository.sourcecraft_id, repository.default_branch
        ) or ""
        return commit_hash
    except SourceCraftError as exc:
        logger.warning(
            f"Не удалось получить hash дефолтной ветки для {repository}: {exc}"
        )
        return ""


def _fallback_health_score(scan_id: int, category: str, reason: str) -> int:
    """Пишет «Нет данных» по категории, если её scan-функция не смогла отработать.

    Используется как при реальных сбоях (категория упала с исключением),
    так и намеренно — например, когда у репозитория issues == 0
    """

    hs, _ = HealthScore.objects.update_or_create(
        scan_id=scan_id,
        category=category,
        defaults=dict(
            total=None,
            weight_used=CATEGORY_WEIGHTS[category],
            data_completeness=0.0,
            raw_metrics={"error": reason},
        ),
    )
    MetricSample.objects.update_or_create(
        scan_id=scan_id,
        category=category,
        metric_key="_category_unavailable",
        defaults=dict(
            value=None,
            is_available=False,
            error_reason=reason[:255],
        ),
    )
    logger.warning(
        f"Категория {category} недоступна для scan={scan_id}: {reason}"
    )
    return hs.id


def _is_effectively_empty(repository: Repository) -> bool:
    """Репозиторий считается пустым, если `is_empty=True`
    ИЛИ у него нет дефолтной ветки — сканировать такой 
    репозиторий по категориям бессмысленно
    """
    return bool(repository.is_empty) or not repository.default_branch


def _create_empty_repo_scan(repository: Repository, triggered_by: str) -> int:
    """Создаёт Scan для пустого репозитория без обращений к SourceCraft API.

    По каждой из 6 категорий пишем HealthScore с `total=None` ("Нет
    данных"), а сам Repo Health Score явно фиксируем как `0`
    """

    reaped = _mark_stale_scans_as_failed(
        Scan.objects.filter(repository=repository)
    )
    if reaped:
        logger.warning(
            f"Репозиторий {repository}: снят зависший Scan ({reaped} шт.)"
        )

    try:
        scan = Scan.objects.create(
            repository=repository,
            status=Scan.Status.SUCCESS,
            triggered_by=triggered_by,
            commit_sha_at_analysis="",
        )
    except IntegrityError as exc:
        raise ActiveScanExistsError(
            f"Активный Scan для репозитория {repository.id} уже существует"
        ) from exc

    reason = (
        "репозиторий пуст is_empty=True"
        if repository.is_empty
        else "у репозитория нет default_branch"
    )

    health_scores = [
        HealthScore(
            scan=scan,
            category=category,
            total=None,
            weight_used=0.0,
            data_completeness=0.0,
            raw_metrics={"reason": reason},
        )
        for category in ALL_CATEGORIES
    ]
    metric_samples = [
        MetricSample(
            scan=scan,
            category=category,
            metric_key="_empty_repository",
            value=None,
            is_available=False,
            error_reason=reason,
        )
        for category in ALL_CATEGORIES
    ]
    HealthScore.objects.bulk_create(health_scores)
    MetricSample.objects.bulk_create(metric_samples)

    scan.finished_at = timezone.now()
    scan.raw = {
        **scan.raw,
        "health_score": 0,
        "category_scores": {c: "Нет данных" for c in ALL_CATEGORIES},
        "missing_categories": [],
        "empty_repository": True,
    }
    scan.save(update_fields=["finished_at", "raw"])

    Repository.objects.filter(pk=repository.pk).update(
        last_scanned_at=scan.finished_at,
        last_commit_sha_processed="",
        health_score=0,
    )

    logger.info(f"Репозиторий {repository} пуст ({reason}) — Score = 0")
    return scan.id


def start_repository_scan(
    repository_id: int,
    triggered_by: str = Scan.TriggeredBy.SCHEDULE,
    force: bool = False,
) -> int:
    """
    Создаёт Scan и раздаёт по одной задаче на каждую из категорий
    параллельно (celery.group), а по их завершении запускает
    task_aggregate_scan (celery.chord callback), который считает
    итоговый Repo Health Score.

    Особые случаи, не требующие полного скана:
    - Пустой репозиторий (`is_empty=True` либо нет `default_branch`):
      категории сразу помечаются "Нет данных", Score = 0.
      Если репозиторий уже был отмечен пустым в прошлый раз и остаётся
      пустым — просто переиспользуем прошлый Scan
    - Категория Issues пропускается, если у репозитория `issues == 0`
    - Если текущий hash дефолтной ветки совпадает
      с `repository.last_commit_sha_processed` и есть хотя бы один
      завершённый скан — репозиторий не менялся, возвращаем id уже
      существующего актуального скана вместо повторного прогона всех категорий.

    `force=True` отключает обе проверки "не изменился" и всегда
    запускает полный скан.
    """
    from health.tasks import task_aggregate_scan

    repository = Repository.objects.get(pk=repository_id)

    if _is_effectively_empty(repository):
        if not force and repository.last_commit_sha_processed == "":
            existing_scan = repository.latest_completed_scan()
            if existing_scan is not None:
                logger.info(
                    f"Репозиторий {repository} пуст — "
                    f"пропускаем пересчёт, используем существующий "
                    f"scan={existing_scan.id}"
                )
                return existing_scan.id
        return _create_empty_repo_scan(repository, triggered_by)

    current_hash = _get_current_commit_hash(repository)
    last_commit = repository.last_commit_sha_processed

    if (
        not force
        and current_hash
        and current_hash == last_commit
    ):
        existing_scan = repository.latest_completed_scan()
        if existing_scan is not None:
            logger.info(
                f"Репозиторий {repository} не изменился с последнего "
                f"скана (commit={current_hash}) — пропускаем пересчёт, "
                f"используем существующий scan={existing_scan.id}"
            )
            return existing_scan.id

    reaped = _mark_stale_scans_as_failed(
        Scan.objects.filter(repository=repository)
    )
    if reaped:
        logger.warning(
            f"Репозиторий {repository}: снят зависший Scan ({reaped} шт.)"
        )

    try:
        scan = Scan.objects.create(
            repository=repository,
            status=Scan.Status.RUNNING,
            triggered_by=triggered_by,
            commit_sha_at_analysis=current_hash or last_commit,
        )
    except IntegrityError as exc:
        raise ActiveScanExistsError(
            f"Активный Scan для репозитория {repository_id} уже существует"
        ) from exc

    category_tasks = [
        task_docs_scan.si(scan.id),
        task_cicd_scan.si(scan.id),
        task_security_scan.si(scan.id),
        task_activity_scan.si(scan.id),
        task_code_health_scan.si(scan.id),
    ]

    if repository.issues > 0:
        category_tasks.append(task_issues_scan.si(scan.id))
    else:
        # У репозитория нет ни одной задачи — категорию не сканируем
        # сразу помечаем "Нет данных".
        _fallback_health_score(
            scan.id,
            MetricSample.Category.ISSUES,
            "в репозитории issues=0 — категория не сканировалась",
        )

    chord(category_tasks)(task_aggregate_scan.s(scan.id))

    return scan.id


def aggregate_scan(scan_id: int) -> dict:
    """
    Считает Repo Health Score по категориям, где данные есть.

    Перенормировка весов: если у части категорий total is None ("Нет
    данных"), их вес НЕ обнуляет итог, а пропорционально
    перераспределяется между категориями, где расчёт есть. Так
    отсутствие данных не ухудшает Score автоматически.
    """

    scan = Scan.objects.select_related("repository").get(pk=scan_id)
    health_scores = list(HealthScore.objects.filter(scan=scan))
    by_category = {hs.category: hs for hs in health_scores}

    # Категория без строки HealthScore — признак сбоя инфраструктуры
    # (задача не доехала до воркера, воркер упал и т.п.), а не "нет данных".
    missing_categories = [c for c in ALL_CATEGORIES if c not in by_category]

    scored = {c: hs for c, hs in by_category.items() if hs.total is not None}
    weight_sum = sum(CATEGORY_WEIGHTS[c] for c in scored)

    if weight_sum > 0:
        overall_score = 0.0
        for category, hs in scored.items():
            renormalized_weight = CATEGORY_WEIGHTS[category] / weight_sum
            overall_score += hs.total * renormalized_weight
            hs.weight_used = renormalized_weight
        overall_score = round(overall_score)
        HealthScore.objects.bulk_update(scored.values(), ["weight_used"])
    else:
        # Ни по одной категории нет данных — Score не считаем
        overall_score = None

    no_data_scores = [hs for c, hs in by_category.items() if c not in scored]
    for hs in no_data_scores:
        hs.weight_used = 0.0
    if no_data_scores:
        HealthScore.objects.bulk_update(no_data_scores, ["weight_used"])

    if missing_categories:
        # Часть категорий не отчиталась вообще — Scan считаем частичным
        scan.status = Scan.Status.PARTIAL
        scan.error = f"Нет результата по категориям: {', '.join(missing_categories)}"
    else:
        scan.status = Scan.Status.SUCCESS

    scan.finished_at = timezone.now()
    scan.raw = {
        **scan.raw,
        "health_score": overall_score,
        "category_scores": {
            c: (
                hs.total if hs.total is not None else "Нет данных"
            ) for c, hs in by_category.items()
        },
        "missing_categories": missing_categories,
    }
    scan.save(update_fields=["status", "finished_at", "raw", "error"])

    repo_update_fields = {
        "health_score": overall_score,
    }
    if scan.commit_sha_at_analysis:
        repo_update_fields["last_scanned_at"] = scan.finished_at
        repo_update_fields["last_commit_sha_processed"] = scan.commit_sha_at_analysis
    Repository.objects.filter(pk=scan.repository_id).update(
        **repo_update_fields
    )

    return {
        "scan_id": scan.id,
        "status": scan.status,
        "health_score": overall_score,
    }


def scan_all_public_repositories():
    """
    Запускает start_repository_scan для каждого публичного репозитория,
    у которого сейчас нет активного (pending/running) Scan. Приватные/
    internal репозитории игнорируем.

    Сканируем в первую очередь репозитории со свежим `last_updated`:
    репозитории без активности (`last_updated` пуст или очень старый)
    ставятся в очередь последними.
    """

    active_repo_ids = Scan.objects.filter(
        status__in=[Scan.Status.PENDING, Scan.Status.RUNNING]
    ).values_list("repository_id", flat=True)

    queryset = (
        Repository.objects.filter(visibility=Repository.VisibilityType.PUBLIC)
        .exclude(pk__in=active_repo_ids)
        .order_by("is_empty", F("last_updated").desc(nulls_last=True))
    )

    repo_ids = list(queryset.values_list("id", flat=True))
    group(
        task_check_and_scan_repository.s(rid) for rid in repo_ids
    ).apply_async()
    return {"dispatched": len(repo_ids)}


def check_and_scan_repository(repository_id: int, force: bool = False) -> dict:
    """Проверка хеша + запуск скана при необходимости"""

    try:
        scan_id = start_repository_scan(
            repository_id,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            force=force,
        )
        return {"repository_id": repository_id, "scan_id": scan_id}
    except ActiveScanExistsError:
        return {"repository_id": repository_id, "skipped": "active"}
    except Repository.DoesNotExist:
        return {"repository_id": repository_id, "skipped": "not_found"}


def fix_stale_scans() -> None:
    reaped = _mark_stale_scans_as_failed(
        Scan.objects.filter(
            status__in=[Scan.Status.PENDING, Scan.Status.RUNNING]
        )
    )
    if reaped:
        logger.warning(f"Переведено в FAILED зависших Scan: {reaped} шт.")
