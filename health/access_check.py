"""Проверка, видит ли токен пользователя репозиторий в SourceCraft.

Страница спрашивает один раз и быстро. Воркер личного скана может ждать
и повторять запрос: его не обрывает таймаут gunicorn.
"""

import logging

import requests
from django.db.models import Q
from requests.adapters import HTTPAdapter

from health.models import Profile, Repository, UserRepositoryAccess
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


def release_access_check(repository_id: int, user_id: int, message: str = "") -> None:
    """Снимает проверку доступа. Scan при этом не создаётся и не меняется."""

    UserRepositoryAccess.objects.filter(
        repository_id=repository_id,
        user_id=user_id,
        status=UserRepositoryAccess.Status.CHECKING,
    ).update(
        status=UserRepositoryAccess.Status.GRANTED,
        checking_since=None,
        check_error=message,
    )


def release_stale_access_checks(threshold) -> int:
    """Снимает проверки доступа, которые висят дольше порога."""

    return UserRepositoryAccess.objects.filter(
        status=UserRepositoryAccess.Status.CHECKING,
    ).filter(
        Q(checking_since__lt=threshold) | Q(checking_since__isnull=True)
    ).update(
        status=UserRepositoryAccess.Status.GRANTED,
        checking_since=None,
        check_error="Проверка доступа зависла и была снята.",
    )


def run_personal_access_check(repository_id: int, user_id: int) -> str:
    """Проверяет доступ и при успехе запускает скан в оркестраторе.

    Возвращает ok, denied, unavailable или gone.
    """

    from django.contrib.auth import get_user_model

    from health.orchestrator import check_and_scan_repository

    access = (
        UserRepositoryAccess.objects.filter(
            repository_id=repository_id,
            user_id=user_id,
            status=UserRepositoryAccess.Status.CHECKING,
        )
        .select_related("repository")
        .first()
    )
    if access is None:
        return "gone"

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        release_access_check(repository_id, user_id, ACCESS_DENIED_TEXT)
        return "denied"

    try:
        confirm_repository_access(user, access.repository)
    except AccessDenied as exc:
        release_access_check(repository_id, user_id, str(exc))
        return "denied"
    except AccessUnavailable:
        return "unavailable"

    still_checking = UserRepositoryAccess.objects.filter(
        pk=access.pk,
        status=UserRepositoryAccess.Status.CHECKING,
    ).exists()
    if not still_checking:
        return "gone"

    check_and_scan_repository(repository_id, force=True, user_id=user_id)
    release_access_check(repository_id, user_id)
    return "ok"
