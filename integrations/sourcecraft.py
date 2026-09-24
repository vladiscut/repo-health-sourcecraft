"""Клиент публичного API SourceCraft.

Модуль инкапсулирует HTTP-доступ к API SourceCraft `https://api.sourcecraft.tech`
Спецификация: `https://api.sourcecraft.tech/sourcecraft.swagger.json`
"""

import logging
import time
from functools import lru_cache
from typing import Any, Iterator, Protocol
from urllib.parse import quote

import redis
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from django.conf import settings


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DEFAULT_PAGE_SIZE = 100

# Лимит SourceCraft API. Применяем не его целиком,
# а с небольшим запасом (RPS_SAFETY_MARGIN)
DEFAULT_RPS_LIMIT = 100
RPS_SAFETY_MARGIN = 0.9
EFFECTIVE_RPS_LIMIT = max(1, int(DEFAULT_RPS_LIMIT * RPS_SAFETY_MARGIN))

# На сколько частей делим каждую секунду при подсчёте лимита в
# RedisRateLimiter. Подсекундные срезы размазывают
# бюджет запросов более равномерно внутри секунды.
RATE_LIMIT_SLICES_PER_SECOND = 10

# Пул соединений HTTP-сессии к SourceCraft
DEFAULT_HTTP_POOL_SIZE = DEFAULT_RPS_LIMIT

# Пул соединений Redis
DEFAULT_REDIS_POOL_SIZE = 64

# Коды ответов, при которых имеет смысл повторять запрос.
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


def _build_retry() -> Retry:
    """Настраивает urllib3.Retry с честным exponential backoff под 429.

    Бюджет ретраев разведён по типам: `status` (сюда попадает 429/5xx)
    получает больше попыток, чем `connect`/ `read` (сетевые проблемы)

    `total=None` — иначе общий счётчик срезал бы status-ретраи раньше,
    чем даст исчерпаться их собственному бюджету.

    `backoff_max` не даёт паузе расти неограниченно при длинной серии
    429 (иначе экспонента 1 -> 2 -> 4 -> 8 ... улетает в минуты)

    `backoff_jitter` размазывает момент повторной попытки
    у разных гринлет/воркеров, чтобы они не ретраили синхронно и
    не создавали новый всплеск ровно к моменту освобождения
    лимита на стороне SourceCraft.

    `respect_retry_after_header=True` приоритетнее экспоненты: если
    SourceCraft в ответе 429 явно прислал `Retry-After`, используется
    именно он, а не расчётный backoff
    """

    kwargs = dict(
        total=None,
        connect=3,
        read=3,
        status=8,
        backoff_factor=1.0,
        backoff_max=60,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    try:
        return Retry(backoff_jitter=1.0, **kwargs)
    except TypeError:
        return Retry(**kwargs)


@lru_cache(maxsize=1)
def _get_rate_limiter(key_prefix: str) -> "RedisRateLimiter":
    """Singleton для RedisRateLimiter, закэширован ПО (key_prefix, max_calls)

    У каждого ресурса — свой ключ в Redis и свой лимит: лимитер
    api, raw, appsec НЕ должны делить один и тот же счётчик запросов.
    """

    redis_client = redis.Redis.from_url(
        settings.CELERY_RESULT_BACKEND,
        max_connections=DEFAULT_REDIS_POOL_SIZE,
    )
    return RedisRateLimiter(
        redis_client,
        max_calls=EFFECTIVE_RPS_LIMIT,
        key_prefix=key_prefix
    )


@lru_cache(maxsize=1)
def _get_shared_session() -> requests.Session:
    adapter = HTTPAdapter(
        max_retries=_build_retry(),
        pool_connections=20,
        pool_maxsize=DEFAULT_HTTP_POOL_SIZE,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class SourceCraftError(RuntimeError):
    """Базовая ошибка при обращении к ресурсам SourceCraft (API или файлам)"""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        error_txt = ""
        if isinstance(payload, dict):
            error_txt = str(payload.get("message", ""))
        elif isinstance(payload, str):
            error_txt = payload
        if error_txt:
            message += f"\n{error_txt}"
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class RateLimiter(Protocol):
    def acquire(self) -> None:
        """Блокирует вызывающий поток, пока не станет можно сделать запрос."""


class RedisRateLimiter:
    """Распределённый лимитер на Redis

    Использует INCR + EXPIRE по ключу `sourcecraft:rl:{slice_index}`, где
    слайс — не целая секунда, а её `slices_per_second` часть (по
    умолчанию 100мс)
    """

    def __init__(
        self,
        redis_client: Any,
        max_calls: int,
        key_prefix: str,
        slices_per_second: int = RATE_LIMIT_SLICES_PER_SECOND,
    ) -> None:
        self.redis = redis_client
        self.max_calls = max_calls
        self.key_prefix = key_prefix
        self.slices_per_second = max(1, slices_per_second)
        self.max_calls_per_slice = max(
            1, -(-max_calls // self.slices_per_second)
        )

    def acquire(self) -> None:
        while True:
            now = time.time()
            slice_index = int(now * self.slices_per_second)
            key = f"{self.key_prefix}:{slice_index}"
            pipe = self.redis.pipeline()
            pipe.incr(key, 1)
            pipe.expire(key, 2)
            count, _ = pipe.execute()
            if count <= self.max_calls_per_slice:
                return
            next_slice_starts_at = (slice_index + 1) / self.slices_per_second
            time.sleep(max(0.0, next_slice_starts_at - time.time()))


class SourceCraftAPI:
    """Общий HTTP-доступ: лимитер, ретраи и пагинация.

    От него наследуются остальные клиенты.
    """

    BASE_URL_SETTING: str = ""
    RATE_LIMIT_KEY_PREFIX: str = ""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        max_pages: int | None = None,
    ) -> None:
        if not self.BASE_URL_SETTING:
            raise NotImplementedError(
                f"{type(self).__name__} должен задать BASE_URL_SETTING"
            )
        if not token:
            token = settings.SOURCECRAFT_API_TOKEN
        self.access_token = token
        self.base_url = self.BASE_URL_SETTING.rstrip("/")
        self.timeout = timeout
        self.session = session or _get_shared_session()
        self.rate_limiter: RateLimiter = rate_limiter or _get_rate_limiter(
            self.RATE_LIMIT_KEY_PREFIX
        )
        self.max_pages = max_pages
        self._owns_session = session is None

    def _user_agent(self) -> str:
        return f"case-18-repo-health-score-team-47 ({type(self).__name__})"

    def _accept_header(self) -> str:
        return "application/json"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": self._accept_header(),
            "User-Agent": self._user_agent(),
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Выполняет запрос и возвращает разобранный JSON"""

        url = f"{self.base_url}{path}"
        clean_params = {
            key: value for key, value in (params or {}).items()
            if value is not None
        }

        self.rate_limiter.acquire()

        try:
            response = self.session.request(
                method,
                url,
                params=clean_params or None,
                json=json_body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning(
                f"Ошибка при запросе: {method} {url} ({exc})"
            )
            raise SourceCraftError(
                f"Ошибка при запросе {url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text[:500]
            logger.warning(
                f"Ошибка в ответе: {method} {url} -> {response.status_code}"
            )
            raise SourceCraftError(
                f"Ответ {response.status_code} для {url}",
                status_code=response.status_code,
                payload=payload,
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SourceCraftError(
                f"Некорректный JSON в ответе {url}: {exc}"
            ) from exc

    def _fetch_page(
        self,
        path: str,
        collection_key: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Загружает одну страницу и возвращает (items, next_page_token)"""

        query = dict(params or {})
        query["page_size"] = page_size
        if page_token:
            query["page_token"] = page_token

        data = self._request("GET", path, params=query) or {}
        items = data.get(collection_key) or []
        return items, data.get("next_page_token")

    def _iter_pages(
        self,
        path: str,
        collection_key: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_page_token: str | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Итерирует страницы, отдавая (items, next_page_token).

        Позволяет обрабатывать данные потоково и сохранять курсор
        `next_page_token` между батчами, не накапливая все страницы в памяти.
        """

        page_token = start_page_token
        seen_tokens: set[str] = set()
        pages_fetched = 0
        while True:
            items, next_token = self._fetch_page(
                path,
                collection_key=collection_key,
                params=params,
                page_size=page_size,
                page_token=page_token,
            )
            yield items, next_token
            pages_fetched += 1

            if not next_token:
                return

            if next_token in seen_tokens:
                logger.error(f"Цикличная пагинация {path}", )
                return
            seen_tokens.add(next_token)
            if self.max_pages is not None and pages_fetched >= self.max_pages:
                logger.warning(
                    f"Пагинация была ограничена max_pages={self.max_pages} для {path}",
                )
                return
            page_token = next_token

    def _paginate(
        self,
        path: str,
        collection_key: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Итерирует элементы пагинации, пока API возвращает next_page_token"""

        for items, _ in self._iter_pages(
            path,
            collection_key=collection_key,
            params=params,
            page_size=page_size,
        ):
            yield from items

    def close(self) -> None:
        """Закрывает HTTP-сессию"""

        if not self._owns_session:
            self.session.close()

    def __enter__(self) -> "SourceCraftAPI":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class SourceCraftClient(SourceCraftAPI):
    """Клиент API SourceCraft"""

    BASE_URL_SETTING = settings.SOURCECRAFT_API_BASE_URL
    RATE_LIMIT_KEY_PREFIX = "sourcecraft:rl"

    def iter_public_repositories(
        self,
        filter_query: str | None = None,
        sort_by: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_page_token: str | None = None,
    ) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
        """Итерирует страницы публичных репозиториев батчами.

        Отдаёт `(items, next_page_token)` для каждой страницы
        """

        params: dict[str, Any] = {}
        if filter_query:
            params["filter"] = filter_query
        if sort_by:
            params["sort_by"] = sort_by
        return self._iter_pages(
            "/repos",
            collection_key="repositories",
            params=params,
            page_size=page_size,
            start_page_token=start_page_token,
        )

    def list_public_repositories(
        self,
        filter_query: str | None = None,
        sort_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает список публичных репозиториев"""

        params: dict[str, Any] = {}
        if filter_query:
            params["filter"] = filter_query
        if sort_by:
            params["sort_by"] = sort_by
        return list(
            self._paginate(
                "/repos",
                collection_key="repositories",
                params=params,
            )
        )

    def get_my_profile(self) -> dict[str, Any]:
        """Возвращает профиль текущего пользователя SourceCraft."""

        return self._request("GET", "/user") or {}

    def get_billing_organization(self) -> dict[str, Any] | None:
        """Организация биллинга CodeAssist, если есть."""

        try:
            return self._request("GET", "/user/code-assist-billing-org") or None
        except SourceCraftError as exc:
            if exc.status_code in {404, 403}:
                return None
            raise

    def list_organization_repositories(self, org_slug: str) -> list[dict[str, Any]]:
        """Возвращает репозитории организации, доступные текущему токену."""

        return list(
            self._paginate(
                f"/orgs/{org_slug}/repos",
                collection_key="repositories",
            )
        )

    def list_accessible_repositories(self) -> list[dict[str, Any]]:
        """Собирает репозитории пользователя: личная орг и биллинг-орг."""

        profile = self.get_my_profile()
        username = profile.get("username") or ""
        org_slugs: list[str] = []
        if username:
            org_slugs.append(username)
        billing = self.get_billing_organization()
        billing_slug = (billing or {}).get("slug")
        if billing_slug and billing_slug not in org_slugs:
            org_slugs.append(billing_slug)

        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for org_slug in org_slugs:
            try:
                items = self.list_organization_repositories(org_slug)
            except SourceCraftError as exc:
                if exc.status_code in {404, 403}:
                    continue
                raise
            for item in items:
                repo_id = item.get("id")
                if not repo_id or repo_id in seen:
                    continue
                seen.add(repo_id)
                result.append(item)
        return result

    def get_repository(self, repo_id: str) -> dict[str, Any]:
        """Возвращает репозиторий по id"""

        data = self._request("GET", f"/repos/id:{repo_id}")
        return data or {}

    def get_repository_file_tree(
        self,
        repo_id: str,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает список файлов репозитория"""

        params: dict[str, Any] = {"revision": since or "HEAD"}
        return list(
            self._paginate(
                f"/repos/id:{repo_id}/trees",
                collection_key="trees",
                params=params,
            )
        )

    def get_ci_pipelines(self, repo_id: str) -> list[dict[str, Any]]:
        """Возвращает запуски CI/CD"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/cicd/runs",
                collection_key="runs",
            )
        )

    def get_issues(self, repo_id: str, state: str | None = None) -> list[dict[str, Any]]:
        """Возвращает задачи репозитория"""

        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        return list(
            self._paginate(
                f"/repos/id:{repo_id}/issues",
                collection_key="issues",
                params=params,
            )
        )

    def get_issue_events(self, repo_id: str, issue_id: str | int) -> list[dict[str, Any]]:
        """Возвращает события/комментарии конкретной задачи.

        Для метрики "время до первого ответа" (категория Issues)"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/issues/{issue_id}/comments",
                collection_key="comments",
            )
        )

    def get_merge_requests(self, repo_id: str) -> list[dict[str, Any]]:
        """Возвращает pull requests репозитория"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/pulls",
                collection_key="pull_requests",
            )
        )

    def get_contributors(self, repo_id: str) -> list[dict[str, Any]]:
        """Возвращает участников репозитория"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/contributors",
                collection_key="contributors",
            )
        )

    def get_releases(self, repo_id: str) -> list[dict[str, Any]]:
        """Возвращает релизы репозитория"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/releases",
                collection_key="releases",
            )
        )

    def get_branches(self, repo_id: str) -> list[dict[str, Any]]:
        """Возвращает ветки репозитория"""

        return list(
            self._paginate(
                f"/repos/id:{repo_id}/branches",
                collection_key="branches",
            )
        )

    def get_default_branch_hash(self, repo_id: str, branch: str) -> str | None:
        """Возвращает hash ветки по умолчанию"""

        collection_key = "branches"
        params = {"filter": branch}
        data = self._request(
            "GET", f"/repos/id:{repo_id}/branches", params=params
        )
        branches = data.get(collection_key)
        if branches:
            return branches[0].get("commit", {}).get("hash")


class SourceCraftFileClient(SourceCraftAPI):
    """Клиент файлового ресурса SourceCraft (сырые файлы репозитория)"""

    BASE_URL_SETTING = settings.SOURCECRAFT_API_FILE_BASE_URL
    RATE_LIMIT_KEY_PREFIX = "sourcecraft:file-rl"

    def _accept_header(self) -> str:
        return "text/plain, */*"

    def get_file_text(
        self, org_slug: str, repo_slug: str, path: str, revision: str
    ) -> str:
        """Возвращает контент одного файла репозитория.

        404 не подменяется пустой строкой/None — поднимается как
        `SourceCraftError(status_code=404)`, вызывающий код сам решает,
        трактовать ли отсутствие файла как "нет данных".
        """

        relative_path = path.lstrip("/")
        if not relative_path or not revision:
            raise SourceCraftError("Для контента файла нужны path и revision")

        if not org_slug or not repo_slug:
            raise SourceCraftError(
                "Для контент файла нужны org_slug и repo_slug"
            )

        url = (
            f"{self.base_url}/raw/"
            f"{quote(org_slug, safe='')}/"
            f"{quote(repo_slug, safe='')}/"
            f"{quote(revision, safe='')}/"
            f"{quote(relative_path, safe='')}"
        )

        self.rate_limiter.acquire()
        try:
            response = self.session.request(
                "GET",
                url,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning(f"Ошибка при запросе: GET {url} ({exc})")
            raise SourceCraftError(
                f"Ошибка при запросе {url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            logger.warning(
                f"Ошибка в ответе: GET {url} -> {response.status_code}"
            )
            raise SourceCraftError(
                f"Ответ {response.status_code} для {url}",
                status_code=response.status_code,
                payload=response.text[:500],
            )
        return response.text
