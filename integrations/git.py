"""Клиент для получения истории коммитов SourceCraft через git-протокол.

Лимитируем не rps, а количество ОДНОВРЕМЕННЫХ клонов (RedisCloneSemaphore),
с TTL-арендой слота на случай падения воркера посреди клонирования.
"""

import logging
import os
import stat
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import redis
from git import GitCommandError, Repo

from django.conf import settings

from integrations.sourcecraft import SourceCraftError

logger = logging.getLogger(__name__)

# Сколько клонов может идти одновременно на весь сервис. git clone —
# тяжёлая операция (диск, сеть, CPU на распаковку),
# поэтому лимитируем именно конкурентность, а не rps.
DEFAULT_MAX_CONCURRENT_CLONES = getattr(
    settings, "SOURCECRAFT_GIT_MAX_CONCURRENT_CLONES", 4
)

# Сколько ждём один git clone, прежде чем считать его ошибкой.
DEFAULT_CLONE_TIMEOUT_SECONDS = getattr(
    settings, "SOURCECRAFT_GIT_CLONE_TIMEOUT_SECONDS", 120
)

# TTL "аренды" слота в семафоре. Должен быть заметно больше таймаута
# клонирования — иначе живой процесс рискует потерять слот раньше,
# чем гарантированно мёртвый успеет протухнуть.
DEFAULT_SEMAPHORE_LEASE_SECONDS = getattr(
    settings,
    "SOURCECRAFT_GIT_SEMAPHORE_LEASE_SECONDS",
    DEFAULT_CLONE_TIMEOUT_SECONDS * 2,
)

DEFAULT_REDIS_POOL_SIZE = 16


class RedisCloneSemaphore:
    """Ограничивает число одновременных git clone через Redis ZSET.

    Каждый держатель слота — член ZSET, score = время захвата. Перед
    каждой попыткой захвата просроченные (старше `lease_seconds`) члены
    вычищаются — это защита от "утечки" слота, если воркер, державший
    его, упал и не успел вызвать release().
    """

    def __init__(
        self,
        redis_client: "redis.Redis",
        max_concurrent: int,
        key: str,
        lease_seconds: float = DEFAULT_SEMAPHORE_LEASE_SECONDS,
    ) -> None:
        self.redis = redis_client
        self.max_concurrent = max(1, max_concurrent)
        self.key = key
        self.lease_seconds = lease_seconds

    def _evict_stale(self, now: float) -> None:
        self.redis.zremrangebyscore(self.key, 0, now - self.lease_seconds)

    def acquire(self, poll_interval: float = 0.5, wait_timeout: float = 600.0) -> str:
        """Блокирует поток, пока не освободится слот, и возвращает его id."""

        member = uuid.uuid4().hex
        waited = 0.0
        while True:
            now = time.time()
            self._evict_stale(now)

            pipe = self.redis.pipeline()
            pipe.zadd(self.key, {member: now})
            pipe.zcard(self.key)
            _, count = pipe.execute()

            if count <= self.max_concurrent:
                return member

            self.redis.zrem(self.key, member)
            if waited >= wait_timeout:
                raise SourceCraftError(
                    f"Не удалось получить слот на git clone за {wait_timeout}с "
                    f"(занято {count}/{self.max_concurrent})"
                )
            time.sleep(poll_interval)
            waited += poll_interval

    def release(self, member: str) -> None:
        self.redis.zrem(self.key, member)

    def lease(self) -> "_SemaphoreLease":
        return _SemaphoreLease(self)


class _SemaphoreLease:
    """Контекстный менеджер: acquire()/release() вокруг блока с git clone."""

    def __init__(self, semaphore: RedisCloneSemaphore) -> None:
        self._semaphore = semaphore
        self._member: str | None = None

    def __enter__(self) -> None:
        self._member = self._semaphore.acquire()
        return None

    def __exit__(self, *exc_info: object) -> None:
        if self._member is not None:
            self._semaphore.release(self._member)


_semaphore_singleton: RedisCloneSemaphore | None = None


def _get_semaphore() -> RedisCloneSemaphore:
    global _semaphore_singleton
    if _semaphore_singleton is None:
        redis_client = redis.Redis.from_url(
            settings.CELERY_RESULT_BACKEND,
            max_connections=DEFAULT_REDIS_POOL_SIZE,
        )
        _semaphore_singleton = RedisCloneSemaphore(
            redis_client,
            max_concurrent=DEFAULT_MAX_CONCURRENT_CLONES,
            key="sourcecraft:git-clone-sem",
        )
    return _semaphore_singleton


def _make_askpass_script() -> Path:
    """Создаёт временный исполняемый askpass-скрипт.

    Скрипт читает GIT_USER / GIT_TOKEN из окружения самого процесса git,
    поэтому токен не попадает ни в argv, ни в лог команды.
    """

    content = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  *sername*) printf "%s" "${GIT_USER:-git}" ;;\n'
        '  *assword*) printf "%s" "${GIT_TOKEN}" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    fd, path_str = tempfile.mkstemp(prefix="git-askpass-", suffix=".sh")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        path.chmod(stat.S_IRWXU)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _build_git_env(token: str, user: str) -> tuple[dict[str, str], Path]:
    askpass = _make_askpass_script()
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(askpass)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_USER"] = user
    env["GIT_TOKEN"] = token
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
    env["GIT_CONFIG_KEY_1"] = "core.askPass"
    env["GIT_CONFIG_VALUE_1"] = str(askpass)
    return env, askpass


class SourceCraftGitClient:
    """Клиент git-доступа к репозиториям SourceCraft.

    По духу — аналог `integrations.sourcecraft.SourceCraftAPI`: токен по
    умолчанию из настроек, единообразная ошибка `SourceCraftError`, общий
    лимитер ресурса (здесь — конкурентность клонов, а не rps), таймаут на
    операцию.
    """

    def __init__(
        self,
        token: str | None = None,
        clone_timeout_seconds: float = DEFAULT_CLONE_TIMEOUT_SECONDS,
        semaphore: RedisCloneSemaphore | None = None,
    ) -> None:
        self.token = token or settings.SOURCECRAFT_API_TOKEN
        self.clone_timeout_seconds = clone_timeout_seconds
        self.semaphore = semaphore or _get_semaphore()

    def _build_clone_url(self, org_slug: str, repo_slug: str) -> str:
        base = settings.SOURCECRAFT_GIT_BASE_URL.rstrip("/")
        return f"{base}/{org_slug}/{repo_slug}.git"

    def get_commit_history(
        self,
        org_slug: str,
        repo_slug: str,
        branch: str,
    ) -> list[datetime]:
        """Клонирует репозиторий и возвращает даты коммитов ветки"""

        if not org_slug or not repo_slug:
            raise SourceCraftError("Для истории коммитов нужны org_slug и repo_slug")
        if not self.token:
            raise SourceCraftError("Нет токена для git-доступа к SourceCraft")

        url = self._build_clone_url(org_slug, repo_slug)

        env, askpass = _build_git_env(self.token, user="anyname")

        try:
            with self.semaphore.lease():
                with tempfile.TemporaryDirectory(prefix="sc-git-") as tmp:
                    clone_kwargs: dict[str, object] = dict(
                        bare=True,
                        single_branch=True,
                        multi_options=[f"--filter=tree:0"],
                        kill_after_timeout=self.clone_timeout_seconds,
                        branch=branch,
                        env=env,
                    )

                    try:
                        repo = Repo.clone_from(url, Path(tmp), **clone_kwargs)
                    except GitCommandError as exc:
                        stderr = (exc.stderr or "").strip()
                        logger.warning(
                            f"git clone не удался для {org_slug}/{repo_slug}: {stderr}"
                        )
                        raise SourceCraftError(
                            f"git clone {org_slug}/{repo_slug} завершился ошибкой",
                            payload=stderr[:500],
                        ) from exc
                    except Exception as exc:
                        logger.warning(
                            f"git clone упал с неожиданной ошибкой для "
                            f"{org_slug}/{repo_slug}: {exc}"
                        )
                        raise SourceCraftError(
                            f"Не удалось клонировать {org_slug}/{repo_slug}: {exc}"
                        ) from exc

                    try:
                        commit_dates = [c.committed_datetime for c in repo.iter_commits()]
                    finally:
                        repo.close()

                    return commit_dates
        finally:
            askpass.unlink(missing_ok=True)
