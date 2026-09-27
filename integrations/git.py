"""Клиент для получения истории коммитов SourceCraft через git-протокол.

Лимитируем не rps, а количество ОДНОВРЕМЕННЫХ клонов (RedisCloneSemaphore),
с TTL-арендой слота на случай падения воркера посреди клонирования.
"""

import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import redis
from git import GitCommandError, Repo

from django.conf import settings

from core.utils import get_scan_repo_dir
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
    settings, "SOURCECRAFT_GIT_CLONE_TIMEOUT_SECONDS", 180  # 3 мин
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

    _BLAME_AUTHOR_TIME_RE = re.compile(r"^author-time (\d+)$")
    _MARKER_RE = re.compile(r"TODO|FIXME|FIX")

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

    def clone(
        self,
        org_slug: str,
        repo_slug: str,
        branch: str,
        scan_id: int,
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
                repo_path = get_scan_repo_dir(scan_id)
                if Path(repo_path).exists() and os.listdir(repo_path):
                    shutil.rmtree(repo_path)
                    repo_path = get_scan_repo_dir(scan_id)

                clone_kwargs: dict[str, object] = dict(
                    single_branch=True,
                    kill_after_timeout=self.clone_timeout_seconds,
                    branch=branch,
                    env=env,
                )

                try:
                    repo = Repo.clone_from(url, repo_path, **clone_kwargs)
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
                finally:
                    repo.close()
        finally:
            askpass.unlink(missing_ok=True)

    def get_commit_history(
        self,
        scan_id: int,
    ) -> list[datetime]:
        """Возвращает даты коммитов"""

        repo_path = get_scan_repo_dir(scan_id)
        repo = None

        try:
            repo = Repo(repo_path)
            if repo.bare:
                raise SourceCraftError(
                    f"Это bare-репозиторий (без рабочей директории файлов) {repo_path}",
                )
            return [c.committed_datetime for c in repo.iter_commits()]
        except SourceCraftError:
            raise
        except Exception as exc:
            raise SourceCraftError(
                f"Не удалось открыть репозиторий по пути {repo_path}: {exc}"
            ) from exc
        finally:
            if repo is not None:
                repo.close()

    @staticmethod
    def scan_markers(repo_path: str) -> list[str]:
        """Возвращает строки с маркерами TODO/FIXME/FIX через `git grep`.

        Формат строки: `<путь>:<номер>:<содержимое>`. Метод статический:
        работает по уже существующему локальному клону, без сети.
        """

        keywords = "|".join(("TODO", "FIXME", "FIX"))
        repo: Repo | None = None
        try:
            repo = Repo(repo_path)
            grep_output = repo.git.grep("-n", "-E", keywords)
            return grep_output.splitlines()
        except GitCommandError as exc:
            # git grep возвращает код 1, когда совпадений нет — это не ошибка.
            if exc.status == 1:
                return []
            raise SourceCraftError(
                f"Не удалось просканировать репозиторий {repo_path}: {exc}"
            ) from exc
        except Exception as exc:
            raise SourceCraftError(
                f"Не удалось просканировать репозиторий {repo_path}: {exc}"
            ) from exc
        finally:
            if repo is not None:
                repo.close()

    @staticmethod
    def scan_marker_positions(repo_path: str) -> list[tuple[str, int, str]]:
        """Возвращает позиции маркеров TODO/FIXME/FIX через `git grep`.

        Каждый элемент — кортеж `(path, line_no, marker)`, где `marker`
        равен `TODO`, `FIXME` или `FIX`. Работает по уже существующему
        локальному клону, без сети.
        """

        results: list[tuple[str, int, str]] = []
        for raw_line in SourceCraftGitClient.scan_markers(repo_path):
            # Формат `git grep -n`: `<путь>:<номер>:<содержимое>`.
            parts = raw_line.split(":", 2)
            if len(parts) < 3:
                continue
            path, line_no_raw, content = parts
            try:
                line_no = int(line_no_raw)
            except ValueError:
                continue
            match = SourceCraftGitClient._MARKER_RE.search(content)
            if match is None:
                continue
            results.append((path, line_no, match.group(0)))
        return results

    def get_marker_commit_dates(
        self,
        scan_id: int,
        branch: str,
    ) -> dict[tuple[str, int], tuple[str, datetime]]:
        """Сопоставляет позиции маркеров TODO/FIXME/FIX с их давностью.

        Для каждого маркера в клоне скана возвращает ключ
        `(path, line_no)` со значением `(marker, committed_datetime)`,
        где дата берётся через `git blame`
        Работает по уже существующему локальному клону, без сети.
        """

        repo_path = get_scan_repo_dir(scan_id)
        positions = SourceCraftGitClient.scan_marker_positions(str(repo_path))
        if not positions:
            return {}

        line_specs: dict[str, list[int]] = {}
        marker_by_pos: dict[tuple[str, int], str] = {}
        for path, line_no, marker in positions:
            line_specs.setdefault(path, []).append(line_no)
            marker_by_pos[(path, line_no)] = marker

        dates_by_file = self.get_line_commit_dates(scan_id, branch, line_specs)

        result: dict[tuple[str, int], tuple[str, datetime]] = {}
        for (path, line_no), marker in marker_by_pos.items():
            committed = dates_by_file.get(path, {}).get(line_no)
            if committed is None:
                continue
            result[(path, line_no)] = (marker, committed)
        return result

    def get_line_commit_dates(
        self,
        scan_id: int,
        branch: str,
        line_specs: dict[str, list[int]],
    ) -> dict[str, dict[int, datetime]]:
        """Для набора (файл -> список номеров строк) возвращает дату
        коммита, последним менявшего каждую строку (`git blame`).

        Используется health.code_health_scan для определения давности
        TODO/FIXME по Git-истории. Работает по уже существующему
        локальному клону, повторно НЕ клонирует.

        Файл, которого нет / который не удалось разобрать, просто
        отсутствует в результирующем словаре — вызывающий код трактует
        это как "давность для этих строк неизвестна", не как ошибку
        всего вызова.
        """

        if not line_specs:
            return {}

        repo_path = get_scan_repo_dir(scan_id)
        repo = None
        result: dict[str, dict[int, datetime]] = {}

        try:
            repo = Repo(repo_path)
            for path, lines in line_specs.items():
                if not lines:
                    continue
                try:
                    dates_by_line = self._blame_lines(repo, branch, path, lines)
                except GitCommandError as exc:
                    logger.warning(
                        f"git blame не удался для "
                        f"{repo_path}:{path}: {exc}"
                    )
                    continue
                if dates_by_line:
                    result[path] = dates_by_line
        except SourceCraftError:
            raise
        except Exception as exc:
            raise SourceCraftError(
                f"Не удалось открыть репозиторий по пути {repo_path}: {exc}"
            ) from exc
        finally:
            if repo is not None:
                repo.close()

        return result

    @staticmethod
    def _blame_lines(
        repo: "Repo",
        revision: str,
        path: str,
        lines: list[int],
    ) -> dict[int, datetime]:
        """Разбирает `git blame --porcelain` за один вызов на файл и
        возвращает {номер_строки: дата_коммита} только для запрошенных
        строк из `lines`.
        """

        wanted = set(lines)
        line_ranges: list[str] = []
        for line_no in sorted(wanted):
            line_ranges.extend(["-L", f"{line_no},{line_no}"])

        output = repo.git.blame(
            "--porcelain",
            *line_ranges,
            revision,
            "--",
            path,
        )

        dates: dict[int, datetime] = {}
        time_by_sha: dict[str, int] = {}
        current_line: int | None = None
        current_sha: str | None = None
        for raw_line in output.splitlines():
            header = raw_line.split(" ")
            if len(header) >= 3 and len(header[0]) == 40:
                # "<sha> <orig_line> <final_line> [<num_lines>]"
                current_sha = header[0]
                try:
                    current_line = int(header[2])
                except ValueError:
                    current_line = None
                cached = time_by_sha.get(current_sha)
                if cached is not None and current_line is not None and current_line in wanted:
                    dates[current_line] = datetime.fromtimestamp(
                        cached, tz=timezone.utc
                    )
                continue
            match = SourceCraftGitClient._BLAME_AUTHOR_TIME_RE.match(raw_line)
            if match and current_sha is not None:
                committed_time = int(match.group(1))
                time_by_sha[current_sha] = committed_time
                if current_line is not None and current_line in wanted:
                    dates[current_line] = datetime.fromtimestamp(
                        committed_time, tz=timezone.utc
                    )
        return dates
