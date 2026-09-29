"""Запуск личного скана: страница только ставит проверку доступа.

Scan здесь не создаётся. Его создаёт оркестратор в start_repository_scan,
когда доступ уже подтверждён.
"""

from django.db import IntegrityError
from django.utils import timezone

from core.celery import USER_QUEUE_NAME
from health.models import Repository, Scan, UserRepositoryAccess
from health.tasks import task_confirm_access_and_scan


ACCESS_CHECK_TEXT = "Проверяем доступ к репозиторию…"
QUEUED_TEXT = "Доступ подтверждён. Анализ поставлен в очередь."
RUNNING_TEXT = "Анализируем…"

PHASE_TEXT = {
    UserRepositoryAccess.Status.CHECKING: ACCESS_CHECK_TEXT,
    Scan.Status.PENDING: QUEUED_TEXT,
    Scan.Status.RUNNING: RUNNING_TEXT,
}


def user_has_saved_access(user, repo: Repository) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return UserRepositoryAccess.objects.filter(
        user=user,
        repository=repo,
    ).exists()


def access_check_in_progress(repo: Repository) -> bool:
    return UserRepositoryAccess.objects.filter(
        repository=repo,
        status=UserRepositoryAccess.Status.CHECKING,
    ).exists()


def access_check_notice(user, repo: Repository) -> str:
    if not getattr(user, "is_authenticated", False):
        return ""
    row = (
        UserRepositoryAccess.objects.filter(user=user, repository=repo)
        .only("check_error")
        .first()
    )
    if row is None:
        return ""
    return row.check_error or ""


def enqueue_personal_scan(user, repo: Repository) -> str:
    """Ставит проверку доступа в очередь analysis.user.

    Возвращает forbidden, active или queued.
    """

    access = UserRepositoryAccess.objects.filter(
        user=user,
        repository=repo,
    ).first()
    if access is None:
        return "forbidden"
    if access_check_in_progress(repo) or Scan.objects.filter(
        repository=repo,
        status__in=Scan.ACTIVE_STATUSES,
    ).exists():
        return "active"
    try:
        updated = UserRepositoryAccess.objects.filter(
            pk=access.pk,
            status=UserRepositoryAccess.Status.GRANTED,
        ).update(
            status=UserRepositoryAccess.Status.CHECKING,
            checking_since=timezone.now(),
            check_error="",
        )
    except IntegrityError:
        return "active"
    if not updated:
        return "active"

    task_confirm_access_and_scan.apply_async(
        kwargs={
            "repository_id": repo.id,
            "user_id": user.id,
        },
        queue=USER_QUEUE_NAME,
    )
    return "queued"
