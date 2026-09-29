"""Запуск скана и сбор итогового балла."""
import datetime
import logging
import shutil
from pathlib import Path

from celery import group, chain

from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.utils import timezone

from core.celery import USER_QUEUE_NAME, SCHEDULE_QUEUE_NAME
from health.models import Finding, HealthScore, MetricSample, Repository, Scan
from health.scoring import CATEGORY_WEIGHTS, overall_finding_impacts
from health.tasks import (
    task_issues_scan,
    task_docs_scan,
    task_cicd_scan,
    task_security_scan,
    task_activity_scan,
    task_code_health_scan,
    task_check_and_scan_repository,
    task_git_clone,
    task_clear_repo_tree,
)
from integrations.sourcecraft import SourceCraftClient, SourceCraftError


logger = logging.getLogger(__name__)

ALL_CATEGORIES = list(CATEGORY_WEIGHTS.keys())

PREVIEW_SCORE_WEIGHT_THRESHOLD = 0.5

SCAN_STALE_AFTER = datetime.timedelta(
    minutes=settings.SCAN_STALE_TIMEOUT_MINUTES
)


class ActiveScanExistsError(Exception):
    """По этому репозиторию уже есть незавершённый Scan."""


def _mark_stale_scans_as_failed(queryset) -> int:
    threshold = timezone.now() - SCAN_STALE_AFTER
    stale_error = (
        f"Scan помечен как зависший: не завершался более {SCAN_STALE_AFTER}"
    )
    return queryset.filter(
        status__in=Scan.ACTIVE_STATUSES,
        created_at__lt=threshold,
    ).update(
        status=Scan.Status.FAILED,
        finished_at=timezone.now(),
        error=stale_error,
    )


def _get_current_commit_hash(repository: Repository) -> str:
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
            source_reference="",
        ),
    )
    logger.warning(
        f"Категория {category} недоступна для scan={scan_id}: {reason}"
    )
    return hs.id


def _is_effectively_empty(repository: Repository) -> bool:
    return bool(repository.is_empty) or not repository.default_branch


def _claim_pending_or_create_running(
    repository: Repository,
    triggered_by: str,
    user_id: int | None,
    commit_sha: str,
) -> Scan:
    """Берёт PENDING-заглушку с карточки или создаёт новый RUNNING Scan."""

    pending = (
        Scan.objects.filter(
            repository=repository,
            status=Scan.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )
    if pending is not None:
        pending.status = Scan.Status.RUNNING
        pending.triggered_by = triggered_by
        pending.triggered_by_user_id = user_id
        pending.commit_sha_at_analysis = commit_sha or ""
        pending.save(
            update_fields=[
                "status",
                "triggered_by",
                "triggered_by_user_id",
                "commit_sha_at_analysis",
            ]
        )
        return pending

    try:
        return Scan.objects.create(
            repository=repository,
            status=Scan.Status.RUNNING,
            triggered_by=triggered_by,
            triggered_by_user_id=user_id,
            commit_sha_at_analysis=commit_sha or "",
        )
    except IntegrityError as exc:
        raise ActiveScanExistsError(
            f"Активный Scan для репозитория {repository.id} уже существует"
        ) from exc


def _create_empty_repo_scan(
    repository: Repository,
    triggered_by: str,
    user_id: int | None
) -> int:
    reaped = _mark_stale_scans_as_failed(
        Scan.objects.filter(repository=repository)
    )
    if reaped:
        logger.warning(
            f"Репозиторий {repository}: снят зависший Scan ({reaped} шт.)"
        )

    pending = (
        Scan.objects.filter(
            repository=repository,
            status=Scan.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )
    if pending is not None:
        scan = pending
        scan.triggered_by = triggered_by
        scan.triggered_by_user_id = user_id
        scan.commit_sha_at_analysis = ""
        scan.status = Scan.Status.SUCCESS
        scan.save(
            update_fields=[
                "triggered_by",
                "triggered_by_user_id",
                "commit_sha_at_analysis",
                "status",
            ]
        )
    else:
        running = Scan.objects.filter(
            repository=repository,
            status=Scan.Status.RUNNING,
        ).exists()
        if running:
            raise ActiveScanExistsError(
                f"Активный Scan для репозитория {repository.id} уже существует"
            )
        scan = Scan.objects.create(
            repository=repository,
            status=Scan.Status.SUCCESS,
            triggered_by=triggered_by,
            triggered_by_user_id=user_id,
            commit_sha_at_analysis="",
        )

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
            # Пустой репозиторий — подтверждающего артефакта нет.
            source_reference="",
        )
        for category in ALL_CATEGORIES
    ]
    HealthScore.objects.bulk_create(health_scores)
    MetricSample.objects.bulk_create(metric_samples)

    scan.finished_at = timezone.now()
    scan.raw = {
        **scan.raw,
        "health_score": None,
        "category_scores": {c: "Нет данных" for c in ALL_CATEGORIES},
        "missing_categories": [],
        "empty_repository": True,
    }
    scan.save(update_fields=["finished_at", "raw"])

    Repository.objects.filter(pk=repository.pk).update(
        last_scanned_at=scan.finished_at,
        last_commit_sha_processed="",
        health_score=None,
    )

    logger.info(f"Репозиторий {repository} пуст ({reason}) — итог «Нет данных»")
    return scan.id


def start_repository_scan(
    repository_id: int,
    force: bool = False,
    user_id: int = None,
) -> int:
    from health.tasks import task_aggregate_scan

    repository = Repository.objects.get(pk=repository_id)

    queue = SCHEDULE_QUEUE_NAME
    triggered_by = Scan.TriggeredBy.SCHEDULE
    if user_id:
        queue = USER_QUEUE_NAME
        triggered_by = Scan.TriggeredBy.USER

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
        return _create_empty_repo_scan(repository, triggered_by, user_id)

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

    scan = _claim_pending_or_create_running(
        repository,
        triggered_by,
        user_id,
        current_hash or last_commit or "",
    )

    category_tasks = []
    if user_id:
        # Если скан запустил пользователь, то сперва получаем клон репозитория
        category_tasks.append(task_git_clone.s(scan.id).set(queue=queue))

    # Публичные категории
    category_tasks.extend([
        task_docs_scan.si(scan.id).set(queue=queue),
        task_code_health_scan.si(scan.id).set(queue=queue),
        task_activity_scan.si(scan.id).set(queue=queue),
    ])

    if repository.issues > 0:
        category_tasks.append(task_issues_scan.si(scan.id).set(queue=queue))
    else:
        # У репозитория нет ни одной задачи — категорию не сканируем
        # сразу помечаем "Нет данных".
        _fallback_health_score(
            scan.id,
            MetricSample.Category.ISSUES,
            "в репозитории issues=0 — категория не сканировалась",
        )

    # Приватные категории
    if user_id:
        category_tasks.append(task_cicd_scan.si(scan.id).set(queue=queue))
        category_tasks.append(task_security_scan.si(scan.id).set(queue=queue))
    else:
        for category in (
            MetricSample.Category.SECURITY,
            MetricSample.Category.CI_CD,
        ):
            _fallback_health_score(
                scan.id,
                category,
                "публичный запуск — категория не сканировалась",
            )

    category_tasks.append(task_clear_repo_tree.si(scan.id).set(queue=queue))
    category_tasks.append(task_aggregate_scan.s(scan.id).set(queue=queue))
    chain(*category_tasks).set(queue=queue).apply_async()

    return scan.id


def _scale_finding_impacts(scan: Scan, by_category: dict[str, HealthScore]) -> None:
    findings = list(Finding.objects.filter(scan=scan))
    if not findings:
        return

    raw = scan.raw or {}
    saved = dict(raw.get("finding_category_points") or {})
    pairs: list[tuple[str, int]] = []
    for finding in findings:
        key = str(finding.id)
        if key not in saved:
            saved[key] = finding.estimated_score_impact
        pairs.append((finding.category, int(saved[key])))

    totals = {category: score.total for category, score in by_category.items()}
    weights = {
        category: score.weight_used for category, score in by_category.items()
    }
    impacts = overall_finding_impacts(pairs, totals, weights)
    for finding, impact in zip(findings, impacts):
        finding.estimated_score_impact = impact
    Finding.objects.bulk_update(findings, ["estimated_score_impact"])

    scan.raw = {**raw, "finding_category_points": saved}


def aggregate_scan(scan_id: int) -> dict:
    scan = Scan.objects.select_related("repository").get(pk=scan_id)
    health_scores = list(HealthScore.objects.filter(scan=scan))
    by_category = {hs.category: hs for hs in health_scores}

    missing_categories = [c for c in ALL_CATEGORIES if c not in by_category]

    scored = {c: hs for c, hs in by_category.items() if hs.total is not None}
    effective_weights = {
        c: CATEGORY_WEIGHTS[c] * hs.data_completeness
        for c, hs in scored.items()
    }
    weight_sum = sum(effective_weights.values())

    if weight_sum > 0:
        overall_score = 0.0
        for category, hs in scored.items():
            renormalized_weight = effective_weights[category] / weight_sum
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

    _scale_finding_impacts(scan, by_category)

    if missing_categories:
        scan.status = Scan.Status.PARTIAL
        scan.error = f"Нет результата по категориям: {', '.join(missing_categories)}"
    else:
        scan.status = Scan.Status.SUCCESS

    total_category_weight = sum(CATEGORY_WEIGHTS[c] for c in ALL_CATEGORIES)
    confidence_numerator = sum(
        CATEGORY_WEIGHTS[c] * hs.data_completeness
        for c, hs in by_category.items()
        if c in CATEGORY_WEIGHTS
    )
    score_confidence = (
        confidence_numerator / total_category_weight
        if total_category_weight > 0
        else 0.0
    )

    scored_weight = sum(CATEGORY_WEIGHTS[c] for c in scored)
    is_preliminary = scored_weight < PREVIEW_SCORE_WEIGHT_THRESHOLD

    scan.finished_at = timezone.now()
    scan.raw = {
        **scan.raw,
        "health_score": overall_score,
        "score_confidence": score_confidence,
        "is_preliminary": is_preliminary,
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
    active_repo_ids = Scan.objects.filter(
        status__in=Scan.ACTIVE_STATUSES
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


def check_and_scan_repository(
    repository_id: int,
    force: bool = False,
    user_id: int = None,
) -> dict:
    """Проверка хеша + запуск скана при необходимости"""

    try:
        scan_id = start_repository_scan(
            repository_id,
            force=force,
            user_id=user_id,
        )
        return {"repository_id": repository_id, "scan_id": scan_id}
    except ActiveScanExistsError:
        return {"repository_id": repository_id, "skipped": "active"}
    except Repository.DoesNotExist:
        return {"repository_id": repository_id, "skipped": "not_found"}


def fix_stale_scans() -> None:
    reaped = _mark_stale_scans_as_failed(
        Scan.objects.filter(status__in=Scan.ACTIVE_STATUSES)
    )
    if reaped:
        logger.warning(f"Переведено в FAILED зависших Scan: {reaped} шт.")
    reap_orphan_clone_dirs()


def reap_orphan_clone_dirs() -> int:
    """Удаляет каталоги клонов, которым больше не соответствует Scan"""

    clone_root = Path(settings.SCAN_REPO_DIR)
    if not clone_root.is_dir():
        return 0

    terminal = (Scan.Status.SUCCESS, Scan.Status.FAILED, Scan.Status.PARTIAL)
    removed = 0
    for entry in clone_root.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue

        scan = Scan.objects.filter(pk=int(entry.name)).only("status").first()
        if scan is not None and scan.status not in terminal:
            # Скан ещё жив — каталог клона нужен, не трогаем.
            continue

        try:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                f"Не удалось удалить каталог клона {entry}: {exc}"
            )

    if removed:
        logger.info(f"Удалено осиротевших каталогов клонов: {removed} шт.")
    return removed
