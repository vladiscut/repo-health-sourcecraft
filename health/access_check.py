"""Проверка, видит ли токен пользователя репозиторий в SourceCraft.

Страница спрашивает один раз и быстро. Воркер личного скана может ждать
и повторять запрос: его не обрывает таймаут gunicorn.
"""

import logging

import requests
from requests.adapters import HTTPAdapter

from health.models import Profile, Repository, Scan, UserRepositoryAccess
from integrations.sourcecraft import SourceCraftClient, SourceCraftError


logger = logging.getLogger(__name__)

PAGE_ACCESS_TIMEOUT = 5.0

ACCESS_DENIED_TEXT = "Нет доступа к репозиторию."
TOKEN_MISSING_TEXT = "Сначала сохраните PAT SourceCraft."
SOURCECRAFT_SILENT_TEXT = "SourceCraft не ответил. Попробуйте ещё раз."
PAGE_UNAVAILABLE_TEXT = "Не удалось проверить доступ, попробуйте ещё раз."


class AccessDenied(Exception):
    """Токен не видит репозиторий, либо его нет."""


class AccessUnavailable(Exception):
    """SourceCraft не ответил. Повтор имеет смысл."""


class PageAccessDenied(Exception):
    """SourceCraft ответил, что доступа нет."""


class PageAccessUnavailable(Exception):
    """Проверка со страницы не дождалась SourceCraft."""


class _NoWaitLimiter:
    def acquire(self) -> None:
        return None


def _token(user) -> str:
    profile = Profile.objects.filter(user=user).first()
    return (profile.sourcecraft_token if profile else None) or ""


def _drop_access(user, repo: Repository) -> None:
    UserRepositoryAccess.objects.filter(user=user, repository=repo).delete()


def _ask(client: SourceCraftClient, user, repo: Repository) -> str:
    try:
        client.get_repository(repo.sourcecraft_id)
    except SourceCraftError as exc:
        if exc.status_code in {403, 404}:
            _drop_access(user, repo)
            logger.info(
                "Доступ к %s/%s снят: %s",
                repo.org_slug,
                repo.repo_slug,
                exc,
            )
            return "denied"
        logger.warning(
            "Не удалось подтвердить доступ к %s/%s: %s",
            repo.org_slug,
            repo.repo_slug,
            exc,
        )
        return "unavailable"
    finally:
        client.close()
    return "ok"


def repository_access_for_page(user, repo: Repository) -> str:
    """Один короткий запрос. Возвращает ok, denied или unavailable."""

    token = _token(user)
    if not token or not repo.sourcecraft_id:
        return "missing"

    session = requests.Session()
    adapter = HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        client = SourceCraftClient(
            token=token,
            timeout=PAGE_ACCESS_TIMEOUT,
            session=session,
            rate_limiter=_NoWaitLimiter(),
        )
        return _ask(client, user, repo)
    finally:
        session.close()


def confirm_repository_access(user, repo: Repository) -> None:
    """Полный запрос для воркера. 403/404 — AccessDenied, сбой — AccessUnavailable."""

    token = _token(user)
    if not token or not repo.sourcecraft_id:
        raise AccessDenied(TOKEN_MISSING_TEXT)

    client = SourceCraftClient(token=token)
    result = _ask(client, user, repo)
    if result == "denied":
        raise AccessDenied(ACCESS_DENIED_TEXT)
    if result == "unavailable":
        raise AccessUnavailable(SOURCECRAFT_SILENT_TEXT)


def fail_personal_scan(repository_id: int, user_id: int, message: str) -> None:
    """Закрывает скан, который ещё не перешёл в выполнение."""

    from django.utils import timezone

    Scan.objects.filter(
        repository_id=repository_id,
        triggered_by_user_id=user_id,
        status__in=[Scan.Status.CHECKING, Scan.Status.PENDING],
    ).update(
        status=Scan.Status.FAILED,
        finished_at=timezone.now(),
        error=message,
    )


def run_personal_access_check(repository_id: int, user_id: int) -> str:
    """Проверяет доступ и при успехе переводит скан из checking в pending.

    Возвращает ok, denied, unavailable или gone.
    """

    from django.contrib.auth import get_user_model

    from health.orchestrator import check_and_scan_repository

    scan = (
        Scan.objects.filter(
            repository_id=repository_id,
            status=Scan.Status.CHECKING,
            triggered_by_user_id=user_id,
        )
        .select_related("repository")
        .first()
    )
    if scan is None:
        return "gone"

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        fail_personal_scan(repository_id, user_id, ACCESS_DENIED_TEXT)
        return "denied"

    try:
        confirm_repository_access(user, scan.repository)
    except AccessDenied as exc:
        fail_personal_scan(repository_id, user_id, str(exc))
        return "denied"
    except AccessUnavailable:
        return "unavailable"

    promoted = Scan.objects.filter(
        pk=scan.pk,
        status=Scan.Status.CHECKING,
    ).update(status=Scan.Status.PENDING)
    if not promoted:
        return "gone"

    check_and_scan_repository(repository_id, force=True, user_id=user_id)
    return "ok"
