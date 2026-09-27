"""Клиент SourceCraft Security API.

База: ``https://appsec.sourcecraft.tech``.
Спецификация: ``https://appsec.sourcecraft.tech/swagger-ui/`` (OpenAPI ``/openapi``).

``gitRepo`` в запросах — это ``sourcecraft_id`` репозитория.
401/403/404 не повторяются и не означают ноль уязвимостей.
429 и 5xx остаются ошибкой после ретраев общей HTTP-сессии.
"""

from typing import Any

from integrations.sourcecraft import (
    DEFAULT_PAGE_SIZE,
    SourceCraftAPI,
    SourceCraftError,
)


APPSEC_PAGE_SIZE = 50
CLIENT_ERROR_CODES = frozenset({401, 403, 404})


class AppSecClientError(SourceCraftError):
    """401, 403 или 404. Это не пустой результат скана."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, payload=None)
        self.payload = payload


class AppSecClient(SourceCraftAPI):
    """Клиент AppSec API."""

    BASE_URL_SETTING_NAME = "SOURCECRAFT_API_APPSEC_BASE_URL"
    RATE_LIMIT_KEY_PREFIX = "sourcecraft:appsec-rl"

    def _user_agent(self) -> str:
        return "case-18-repo-health-score-team-47 (AppSecClient)"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return super()._request(
                method,
                path,
                params=params,
                json_body=json_body,
            )
        except SourceCraftError as exc:
            if exc.status_code in CLIENT_ERROR_CODES:
                raise AppSecClientError(
                    str(exc),
                    status_code=exc.status_code,
                    payload=exc.payload,
                ) from exc
            raise

    def _fetch_page(
        self,
        path: str,
        collection_key: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Страница AppSec: ``pageSize`` / ``pageToken`` / ``nextPageToken``."""

        query = dict(params or {})
        query["pageSize"] = page_size
        if page_token:
            query["pageToken"] = page_token

        data = self._request("GET", path, params=query) or {}
        items = data.get(collection_key) or []
        return items, data.get("nextPageToken") or None

    def get_latest_scan(self, sourcecraft_id: str) -> dict[str, Any] | None:
        """Последний скан. ``GET /v1/scans/latest?gitRepo=``.

        Пустой список — ``None``. Это не скан со статусом FINISHED и нулём находок.
        """

        data = self._request(
            "GET",
            "/v1/scans/latest",
            params={"gitRepo": sourcecraft_id},
        )
        if not isinstance(data, dict):
            return None

        scans = data.get("data")
        if isinstance(scans, list):
            if not scans:
                return None
            for item in scans:
                if isinstance(item, dict) and item.get("isLatest"):
                    return item
            first = scans[0]
            return first if isinstance(first, dict) else None

        if not data.get("status") and not data.get("uuid"):
            return None
        return data

    def list_defect_groups(
        self,
        sourcecraft_id: str,
        scan_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Группы дефектов. ``GET /v1/defect-groups?gitRepo=``."""

        params: dict[str, Any] = {"gitRepo": sourcecraft_id}
        if scan_uuid:
            params["scanUuid"] = scan_uuid
        return list(
            self._paginate(
                "/v1/defect-groups",
                collection_key="data",
                params=params,
                page_size=APPSEC_PAGE_SIZE,
            )
        )

    def list_findings(
        self,
        sourcecraft_id: str,
        defect_group_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Находки. ``GET /v1/findings?gitRepo=``."""

        params: dict[str, Any] = {"gitRepo": sourcecraft_id}
        if defect_group_uuid:
            params["defectGroupUuid"] = defect_group_uuid
        return list(
            self._paginate(
                "/v1/findings",
                collection_key="data",
                params=params,
                page_size=APPSEC_PAGE_SIZE,
            )
        )