from celery import shared_task
from celery.signals import worker_ready

from django.utils import timezone

from core.utils import parse_datetime
from health.models import Repository, MetricSample
from integrations.sourcecraft import SourceCraftError as APIErr


@worker_ready.connect
def task_worker_ready_update_all_public_repos(sender, **kwargs) -> None:
    """Запускает получение всех публичных репозиториев при
    старте сервиса, если в БД пусто"""

    if Repository.objects.all().exists():
        return

    with sender.app.connection() as conn:
        sender.app.send_task(
            'health.tasks.task_update_all_public_repos',
            connection=conn
        )


@shared_task
def task_update_all_public_repos(
    page_token: str | None = None,
    sync_started_at: str | None = None,
) -> None:
    """Обрабатывает страницы с репозиториями порциями и, если есть
    еще страница, ставит следующую задачу с курсором `page_token`"""

    from health.repository import update_all_public_repositories

    sync_dt = parse_datetime(sync_started_at) if sync_started_at else timezone.now()

    next_token, sync_dt = update_all_public_repositories(
        start_page_token=page_token,
        sync_started_at=sync_dt,
    )
    if next_token:
        task_update_all_public_repos.apply_async(
            args=[next_token, sync_dt.isoformat()]
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_issues_scan(self, scan_id: int) -> int:
    """Прогоняет категорию ISSUES для существующего Scan"""

    from health.issues_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.ISSUES, str(exc)
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_docs_scan(self, scan_id: int) -> int:
    """Прогоняет категорию DOCS для существующего Scan"""

    from health.docs_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.DOCS, str(exc)
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_cicd_scan(self, scan_id: int) -> int:
    """Прогоняет категорию CI_CD для существующего Scan"""

    from health.cicd_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.CI_CD, str(exc)
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_security_scan(self, scan_id: int) -> int:
    """Прогоняет категорию SECURITY для существующего Scan"""

    from health.security_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.SECURITY, str(exc)
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_activity_scan(self, scan_id: int) -> int:
    """Прогоняет категорию ACTIVITY для существующего Scan"""

    from health.activity_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.ACTIVITY, str(exc)
        )


@shared_task(
    bind=True, max_retries=3, default_retry_delay=30, autoretry_for=(APIErr,),
)
def task_code_health_scan(self, scan_id: int) -> int:
    """Прогоняет категорию CODE_HEALTH для существующего Scan"""

    from health.code_health_scan import run
    from health.orchestrator import _fallback_health_score

    try:
        return run(scan_id)
    except APIErr:
        raise
    except Exception as exc:
        return _fallback_health_score(
            scan_id, MetricSample.Category.CODE_HEALTH, str(exc)
        )


@shared_task(bind=True, max_retries=2, default_retry_delay=15)
def task_aggregate_scan(self, category_healthscore_ids, scan_id: int) -> None:
    """Считает Repo Health Score по категориям"""

    from health.orchestrator import aggregate_scan
    aggregate_scan(scan_id)


@shared_task
def task_scan_all_public_repositories() -> None:
    """Запускает сканирование для каждого публичного репозитория"""

    from health.orchestrator import scan_all_public_repositories
    scan_all_public_repositories()


@shared_task
def task_reap_stale_scans() -> None:
    """
    Проходит по всем репозиториям и переводит в FAILED любой Scan,
    провисевший в PENDING/RUNNING дольше SCAN_STALE_AFTER.
    """

    from health.orchestrator import fix_stale_scans
    fix_stale_scans()


@shared_task
def task_check_and_scan_repository(repository_id: int) -> None:
    """Проверка хеша + запуск скана при необходимости для олного репозитория"""

    from health.orchestrator import check_and_scan_repository
    check_and_scan_repository(repository_id)


@shared_task
def task_scan_user_repository(repository_id: int) -> None:
    """Скан репозитория пользователя"""

    from health.user_repository import scan_user_repository
    scan_user_repository(repository_id)
