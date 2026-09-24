import logging

from django.contrib.auth import get_user_model

from health.models import Profile, Repository, UserRepositoryAccess
from health.repository import prepare_repositories_data, _flush_repositories
from integrations.sourcecraft import SourceCraftClient, SourceCraftError
from integrations.yandex import token_expires_at


logger = logging.getLogger(__name__)


def sync_user_repositories(profile: Profile) -> int:
    """Тянет доступные пользователю репозитории SourceCraft и пишет доступ."""

    token = profile.sourcecraft_token
    if not token:
        raise SourceCraftError("Нет токена SourceCraft")

    client = SourceCraftClient(token=token)
    try:
        me = client.get_my_profile()
        username = me.get("username") or ""
        if username and profile.sourcecraft_username != username:
            profile.sourcecraft_username = username
            profile.save(update_fields=["sourcecraft_username"])

        payloads = prepare_repositories_data(
            client.list_accessible_repositories()
        )

        _flush_repositories([Repository(**item) for item in payloads if item])

        ids = [item["sourcecraft_id"] for item in payloads]
        repos = list(Repository.objects.filter(sourcecraft_id__in=ids))

        repo_ids = [repo.id for repo in repos]
        UserRepositoryAccess.objects.filter(user=profile.user).exclude(
            repository_id__in=repo_ids
        ).delete()
        UserRepositoryAccess.objects.bulk_create(
            [
                UserRepositoryAccess(user=profile.user, repository=repo)
                for repo in repos
            ],
            ignore_conflicts=True,
        )
        return len(repos)
    finally:
        client.close()


def user_can_access_repository(user, repo: Repository) -> bool:
    """Спрашивает SourceCraft, видит ли токен пользователя репозиторий сейчас.

    Локальная связь UserRepositoryAccess для этого не годится: человека могли
    убрать из команды или снять право на просмотр уже после синхронизации.
    """

    if not getattr(user, "is_authenticated", False):
        return False

    profile = Profile.objects.filter(user=user).first()
    token = profile.sourcecraft_token if profile else None
    if not token or not repo.sourcecraft_id:
        return False

    client = SourceCraftClient(token=token)
    try:
        client.get_repository(repo.sourcecraft_id)
    except SourceCraftError as exc:
        if exc.status_code in {403, 404}:
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=repo,
            ).delete()
            logger.info(
                "Доступ к %s/%s снят: %s",
                repo.org_slug,
                repo.repo_slug,
                exc,
            )
        else:
            logger.warning(
                "Не удалось подтвердить доступ к %s/%s: %s",
                repo.org_slug,
                repo.repo_slug,
                exc,
            )
        return False
    finally:
        client.close()
    return True


def sourcecraft_token_works(token: str) -> tuple[bool, str]:
    """Проверяет, что токен ходит в API SourceCraft. Возвращает (ok, username)."""

    if not token:
        return False, ""

    client = SourceCraftClient(token=token)
    try:
        me = client.get_my_profile()
        return True, me.get("username") or ""
    except SourceCraftError as exc:
        logger.info("Токен SourceCraft отклонён: %s", exc)
        return False, ""
    finally:
        client.close()


def upsert_yandex_user(info: dict, tokens: dict) -> object:
    """Создаёт или обновляет пользователя Django по ответу Я ID."""

    user_model = get_user_model()
    ya_id = str(info.get("id") or "")
    if not ya_id:
        raise ValueError("В ответе Я ID нет id")

    login = (info.get("login") or f"ya_{ya_id}")[:150]
    email = info.get("default_email") or ""
    first_name = (info.get("first_name") or "")[:150]
    last_name = (info.get("last_name") or "")[:150]
    expires_at = token_expires_at(tokens.get("expires_in"))

    profile = Profile.objects.filter(ya_id=ya_id).select_related("user").first()
    if profile:
        user = profile.user
    else:
        username = login
        if user_model.objects.filter(username=username).exists():
            username = f"{login}_{ya_id[:8]}"[:150]
        user = user_model(username=username, email=email)
        user.set_unusable_password()
        user.save()
        profile = Profile(user=user, ya_id=ya_id)

    user.email = email or user.email
    user.first_name = first_name
    user.last_name = last_name
    user.save(update_fields=["email", "first_name", "last_name"])

    profile.access_token = tokens.get("access_token") or None
    profile.refresh_token = tokens.get("refresh_token") or None
    profile.token_expires_at = expires_at
    profile.save()
    return user


def scan_user_repository(repository_id: int) -> None:
    # TODO
    # реализовать scan_repository в health/repository.py
    # импортировать и использовать тут
    pass
