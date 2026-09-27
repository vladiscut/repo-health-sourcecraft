"""Общий кэш дерева файлов репозитория для категорий, которые читают
дерево через SourceCraftClient.get_repository_file_tree (сейчас — Docs
и Code Health).

Внутри одного Scan обе категории — публичные, идут в одном celery.group
(см. health.orchestrator.start_repository_scan) и выполняются
ПАРАЛЛЕЛЬНО на одном и том же (repo_id, revision). Без кэша это два
одинаковых запроса к SourceCraft почти одновременно.

Решение: Redis-кэш дерева с TTL + single-flight блокировка на случай,
если оба воркера пришли одновременно и кэш ещё пуст — тогда идёт в API
только тот, кто первым захватил лок, второй ждёт и читает готовый
результат. Тот же принцип (SET NX + TTL-аренда), что и в
integrations.git.RedisCloneSemaphore и в
integrations.sourcecraft.RedisRateLimiter — просто здесь это кэш
результата, а не лимитер конкурентности.

Ошибка API намеренно НЕ кэшируется: транзитный сбой не должен
"заморозить" пустой результат на весь TTL для обеих категорий сразу —
каждая пусть сама решает, как трактовать недоступность дерева
(см. _fallback-логику в docs_scan.py / code_health_scan.py).
"""

import json
import logging
import time
from functools import lru_cache
from typing import Any

import redis

from django.conf import settings

from health.models import Repository
from integrations.sourcecraft import SourceCraftClient, SourceCraftError


logger = logging.getLogger(__name__)

TREE_CACHE_TTL_SECONDS = getattr(settings, "REPO_TREE_CACHE_TTL_SECONDS", 600)

# Локально короткий TTL: если воркер с локом упал, второй не должен
# ждать вечно.
TREE_CACHE_LOCK_TTL_SECONDS = 30
TREE_CACHE_LOCK_POLL_INTERVAL = 0.5
TREE_CACHE_LOCK_WAIT_TIMEOUT = 60.0

TREE_CACHE_KEY_PREFIX = "sourcecraft:tree-cache"
TREE_CACHE_LOCK_PREFIX = "sourcecraft:tree-cache-lock"

DEFAULT_REDIS_POOL_SIZE = 16


@lru_cache(maxsize=1)
def _get_redis() -> "redis.Redis":
    return redis.Redis.from_url(
        settings.REDIS_TREE_CACHE_URL,
        max_connections=DEFAULT_REDIS_POOL_SIZE,
    )


def _cache_key(repo_id: str, revision: str | None) -> str:
    return f"{TREE_CACHE_KEY_PREFIX}:{repo_id}:{revision or 'HEAD'}"


def _lock_key(repo_id: str, revision: str | None) -> str:
    return f"{TREE_CACHE_LOCK_PREFIX}:{repo_id}:{revision or 'HEAD'}"


def get_repository_tree_cached(
    client: SourceCraftClient,
    repository: Repository,
) -> list[dict[str, Any]]:
    """Возвращает дерево файлов репозитория, используя общий Redis-кэш.

    `repository` — health.models.Repository (не типизируем явно во
    избежание циклического импорта health.models <-> health.tree_cache).

    Поднимает SourceCraftError, если дерево получить не удалось —
    вызывающий код (docs_scan/code_health_scan) сам решает, как
    трактовать отсутствие данных, как и раньше.
    """
    print('============== get_repository_tree_cached')
    redis_client = _get_redis()
    revision = repository.default_branch or None
    key = _cache_key(repository.sourcecraft_id, revision)

    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)

    lock_key = _lock_key(repository.sourcecraft_id, revision)
    got_lock = bool(
        redis_client.set(
            lock_key, "1", nx=True, ex=TREE_CACHE_LOCK_TTL_SECONDS
        )
    )

    if not got_lock:
        waited = 0.0
        while waited < TREE_CACHE_LOCK_WAIT_TIMEOUT:
            time.sleep(TREE_CACHE_LOCK_POLL_INTERVAL)
            waited += TREE_CACHE_LOCK_POLL_INTERVAL
            cached = redis_client.get(key)
            if cached is not None:
                return json.loads(cached)
        # Не дождались (лок утёк, но значения так и нет) — не блокируем
        # категорию навсегда, идём в API сами, без повторного лока.
        logger.warning(
            f"Не дождались tree-кэша для {repository} — идём в API напрямую"
        )

    try:
        tree = client.get_repository_file_tree(
            repository.sourcecraft_id, revision
        )
    except SourceCraftError:
        if got_lock:
            redis_client.delete(lock_key)
        raise

    try:
        redis_client.set(key, json.dumps(tree), ex=TREE_CACHE_TTL_SECONDS)
    finally:
        if got_lock:
            redis_client.delete(lock_key)

    return tree


def clear_repository_tree_cache(repository: Any) -> None:
    """Удаляет запись кэша дерева для репозитория.

    Вызывается явной clear-задачей сразу после последнего потребителя
    дерева в цепочке скана, чтобы не держать устаревшее дерево весь TTL.
    """

    redis_client = _get_redis()
    revision = repository.default_branch or None
    redis_client.delete(_cache_key(repository.sourcecraft_id, revision))
