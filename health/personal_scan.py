"""Запуск личного скана: страница только создаёт проверку доступа."""

from django.db import IntegrityError

from core.celery import USER_QUEUE_NAME
from health.models import Repository, Scan, UserRepositoryAccess
from health.tasks import task_confirm_access_and_scan


ACCESS_CHECK_TEXT = "Проверяем доступ к репозиторию…"
QUEUED_TEXT = "Доступ подтверждён. Анализ поставлен в очередь."
RUNNING_TEXT = "Анализируем…"

PHASE_TEXT = {
    Scan.Status.CHECKING: ACCESS_CHECK_TEXT,
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


def enqueue_personal_scan(user, repo: Repository) -> str:
    """Ставит проверку доступа в очередь analysis.user.

    Возвращает forbidden, active или queued.
    """

    if not user_has_saved_access(user, repo):
        return "forbidden"
    if Scan.objects.filter(
        repository=repo,
        status__in=Scan.ACTIVE_STATUSES,
    ).exists():
        return "active"
    try:
        Scan.objects.create(
            repository=repo,
            status=Scan.Status.CHECKING,
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=user,
        )
    except IntegrityError:
        return "active"

    task_confirm_access_and_scan.apply_async(
        kwargs={
            "repository_id": repo.id,
            "user_id": user.id,
        },
        queue=USER_QUEUE_NAME,
    )
    return "queued"
