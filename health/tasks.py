from celery import shared_task
from celery.signals import worker_ready

from django.utils import timezone

from core.utils import parse_datetime
from health.models import Repository, MetricSample
from integrations.sourcecraft import SourceCraftError as APIErr


@worker_ready.connect
def task_worker_ready_update_all_public_repos(sender, **kwargs) -> None:
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
def task_aggregate_scan(self, _category_healthscore_ids, scan_id: int) -> None:
    from health.orchestrator import aggregate_scan
    aggregate_scan(scan_id)


@shared_task
def task_scan_all_public_repositories() -> None:
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
def task_reap_orphan_clone_dirs() -> int:
    from health.orchestrator import reap_orphan_clone_dirs
    return reap_orphan_clone_dirs()


def _fail_unclaimed_pending(repository_id: int, exc: BaseException) -> None:
    """Закрывает PENDING, если запуск упал до того, как оркестратор забрал скан."""

    from django.utils import timezone

    from health.models import Scan

    Scan.objects.filter(
        repository_id=repository_id,
        status=Scan.Status.PENDING,
    ).update(
        status=Scan.Status.FAILED,
        finished_at=timezone.now(),
        error=f"Запуск анализа не удался: {exc}",
    )


@shared_task
def task_check_and_scan_repository(
    repository_id: int,
    user_id: int = None,
    force: bool = False,
) -> None:
    try:
        from health.orchestrator import check_and_scan_repository
        check_and_scan_repository(repository_id, force, user_id)
    except Exception as exc:
        _fail_unclaimed_pending(repository_id, exc)
        raise


@shared_task(bind=True, max_retries=2, default_retry_delay=20)
def task_confirm_access_and_scan(
    self,
    repository_id: int,
    user_id: int,
) -> None:
    from health.access_check import (
        SOURCECRAFT_SILENT_TEXT,
        release_access_check,
        run_personal_access_check,
    )

    try:
        outcome = run_personal_access_check(repository_id, user_id)
    except Exception as exc:
        release_access_check(
            repository_id,
            user_id,
            f"Запуск анализа не удался: {exc}",
        )
        raise

    if outcome != "unavailable":
        return
    if self.request.retries >= self.max_retries:
        release_access_check(repository_id, user_id, SOURCECRAFT_SILENT_TEXT)
        return
    raise self.retry()


@shared_task(bind=True, max_retries=2, default_retry_delay=15)
def task_git_clone(self, scan_id: int) -> list[int]:
    from health.git_clone import run
    run(scan_id)


@shared_task
def task_clear_repo_tree(scan_id: int) -> None:
    from health.models import Scan
    from health.tree_cache import clear_repository_tree_cache

    scan = Scan.objects.select_related("repository").filter(pk=scan_id).first()
    if scan:
        clear_repository_tree_cache(scan.repository)
