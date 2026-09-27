import logging

from datetime import datetime

from django.utils import timezone

from core.utils import parse_datetime, to_int, to_float
from health.models import Repository
from integrations.sourcecraft import SourceCraftClient


logger = logging.getLogger(__name__)


REPOSITORY_SYNC_FIELDS = [
    "org_slug",
    "repo_slug",
    "description",
    "language",
    "rating_value",
    "rating_percentile",
    "likes",
    "hearts",
    "diamonds",
    "forks",
    "url",
    "logo_url",
    "is_empty",
    "last_updated",
    "visibility",
    "updated_at",
    "default_branch",
    "issues",
]


def _reaction_counts(payload: dict) -> dict[str, int]:
    counts = {"likes": 0, "hearts": 0, "diamonds": 0}
    mapping = {
        "positive_low": "likes",
        "positive_medium": "hearts",
        "positive_high": "diamonds",
    }
    rating = payload.get("rating", {})
    for item in rating.get("reaction_counts", []):
        field = mapping.get(item.get("type"))
        if field:
            counts[field] = to_int(item.get("count"))
    return counts


def _language_name(value: object) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def prepare_repository_data(payload: dict) -> dict | None:
    """Подготавливает данные для одного объекта JSON"""

    try:
        organization = payload.get("organization", {})
        if not isinstance(organization, dict):
            organization = {}
        logo = payload.get("logo", {})
        if not isinstance(logo, dict):
            logo = {}
        counters = payload.get("counters", {})
        if not isinstance(counters, dict):
            counters = {}
        rating = payload.get("rating", {})
        if not isinstance(rating, dict):
            rating = {}
        reactions = _reaction_counts(payload)
        org_slug = organization.get("slug")
        repo_slug = payload.get("slug")
        sourcecraft_id = payload.get("id")
        url = payload.get("web_url")
        if not org_slug or not repo_slug or not sourcecraft_id or not url:
            return None

        return {
            "org_slug": org_slug,
            "repo_slug": repo_slug,
            "description": payload.get("description", ""),
            "language": _language_name(payload.get("language")),
            "rating_value": to_float(rating.get("value")) or 0,
            "rating_percentile": to_float(rating.get("percentile")),
            "likes": reactions["likes"],
            "hearts": reactions["hearts"],
            "diamonds": reactions["diamonds"],
            "forks": to_int(counters.get("forks")),
            "issues": to_int(counters.get("issues")),
            "sourcecraft_id": sourcecraft_id,
            "url": url,
            "logo_url": logo.get("url"),
            "is_empty": bool(payload.get("is_empty")),
            "last_updated": parse_datetime(payload.get("last_updated")),
            "visibility": payload.get("visibility"),
            "default_branch": payload.get("default_branch"),
        }
    except Exception as e:
        logger.error(f"Ошибка при подготовке данных из объекта JSON: {e}")


def prepare_repositories_data(payload: list) -> list:
    """Подготавливает данные из списка JSON объектов из ответа SourceCraft API"""

    return list(
        filter(
            None,
            [prepare_repository_data(i) for i in payload]
        )
    )


def _flush_repositories(buffer: list[Repository]) -> None:
    """Сбрасывает накопленные объекты в БД и очищает буфер"""

    if not buffer:
        return

    Repository.objects.bulk_create(
        buffer,
        update_conflicts=True,
        unique_fields=["sourcecraft_id"],
        update_fields=REPOSITORY_SYNC_FIELDS,
        batch_size=1000,
    )
    buffer.clear()


def cleanup_repositories_missing_since(sync_started_at: datetime) -> int:
    """Удаляет публичные репозитории, не встретившиеся ни на одной
    странице при полной синхронизации.

    Вызывать только после того, как пагинация полностью пройдена до
    конца (next_page_token is None) — иначе можно удалить репозитории,
    до страницы которых просто ещё не дошли.
    """

    deleted, _ = Repository.objects.filter(
        visibility=Repository.VisibilityType.PUBLIC,
        updated_at__lt=sync_started_at,
    ).delete()
    if deleted:
        logger.info(
            f"Удалено репозиториев, пропавших из выдачи API: {deleted}"
        )
    return deleted


def update_all_public_repositories(
    start_page_token: str | None = None,
    sync_started_at: datetime | None = None,
) -> tuple[str | None, datetime]:
    """Создаёт или обновляет объекты `Repository` пачками страниц

    Не собирает все страницы в память: накапливает до `pages_per_batch`
    страниц, сбрасывает их в БД и продолжает со следующей страницы.

    `sync_started_at` — момент начала ВСЕЙ синхронизации (а не только
    текущего батча страниц). Должен быть зафиксирован до первого вызова
    и передаваться неизменным при каждом последующем самопланировании
    задачи с курсором. Когда пагинация заканчивается (next_page_token
    is None), функция считает синхронизацию завершённой и удаляет из
    БД репозитории, не встретившиеся ни на одной странице

    Возвращает ``(next_page_token, sync_started_at)`` — оба значения
    нужно передать в следующий вызов, если есть next_page_token.
    """

    if sync_started_at is None:
        sync_started_at = timezone.now()

    client = SourceCraftClient()
    buffer: list[Repository] = []
    # Сколько страниц API накапливать перед сбросом в БД
    pages_per_batch = 10  # т.е. pages_per_batch * 100 репозиториев за раз
    # Сколько страниц в пачке
    pages_in_batch = 0

    try:
        for items, next_token in client.iter_public_repositories(
            start_page_token=start_page_token,
        ):
            for item in items:
                data = prepare_repository_data(item)
                if data:
                    buffer.append(Repository(**data))

            pages_in_batch += 1
            if pages_in_batch >= pages_per_batch:
                _flush_repositories(buffer)
                return next_token, sync_started_at

        _flush_repositories(buffer)
        cleanup_repositories_missing_since(sync_started_at)
        return None, sync_started_at
    finally:
        client.close()
