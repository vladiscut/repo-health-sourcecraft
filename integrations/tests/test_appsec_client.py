from unittest.mock import Mock

from django.conf import settings
from django.test import SimpleTestCase

from integrations.appsec import AppSecClient, AppSecClientError
from integrations.sourcecraft import SourceCraftError


class _NoLimit:
    def acquire(self) -> None:
        return None


def _response(status: int, body: object) -> Mock:
    response = Mock()
    response.status_code = status
    response.content = b"{}"
    response.json.return_value = body
    response.text = ""
    return response


FINISHED_SCAN = {
    "uuid": "01890f3e-7b5c-7cc2-bc6f-3f5d8c9a1e4a",
    "status": "FINISHED",
    "totalDefectGroups": 0,
    "foundDefectGroups": 0,
}

EMPTY_PAGE = {"data": [], "nextPageToken": "", "totalSize": 0}


class AppSecClientTests(SimpleTestCase):
    def _client(self, handler) -> tuple[AppSecClient, Mock]:
        session = Mock()
        session.request.side_effect = handler
        client = AppSecClient(
            token="token",
            session=session,
            rate_limiter=_NoLimit(),
        )
        return client, session

    def test_finished_without_findings_keeps_the_scan(self):
        def handler(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(kwargs["params"]["gitRepo"], "repo-1")
            if url.endswith("/v1/scans/latest"):
                return _response(200, FINISHED_SCAN)
            if url.endswith("/v1/defect-groups"):
                self.assertEqual(kwargs["params"]["pageSize"], 50)
                return _response(200, EMPTY_PAGE)
            if url.endswith("/v1/findings"):
                return _response(200, EMPTY_PAGE)
            raise AssertionError(url)

        client, session = self._client(handler)
        scan = client.get_latest_scan("repo-1")
        groups = client.list_defect_groups("repo-1", scan_uuid=FINISHED_SCAN["uuid"])
        findings = client.list_findings("repo-1")

        self.assertEqual(scan, FINISHED_SCAN)
        self.assertEqual(groups, [])
        self.assertEqual(findings, [])
        self.assertEqual(session.request.call_count, 3)
        group_params = session.request.call_args_list[1].kwargs["params"]
        self.assertEqual(group_params["scanUuid"], FINISHED_SCAN["uuid"])
        self.assertTrue(
            session.request.call_args_list[0].args[1].startswith(
                settings.SOURCECRAFT_API_APPSEC_BASE_URL
            )
        )

    def test_forbidden_is_not_an_empty_result(self):
        def handler(method, url, **kwargs):
            return _response(403, {"message": "forbidden"})

        client, _session = self._client(handler)

        with self.assertRaises(AppSecClientError) as ctx:
            client.get_latest_scan("repo-1")

        self.assertEqual(ctx.exception.status_code, 403)

    def test_empty_scan_list_is_not_zero_findings(self):
        def handler(method, url, **kwargs):
            self.assertTrue(url.endswith("/v1/scans/latest"))
            return _response(200, EMPTY_PAGE)

        client, session = self._client(handler)

        scan = client.get_latest_scan("repo-1")

        self.assertIsNone(scan)
        self.assertEqual(session.request.call_count, 1)

    def test_rate_limit_stays_separate_from_client_errors(self):
        def handler(method, url, **kwargs):
            return _response(429, {"message": "slow down"})

        client, _session = self._client(handler)

        with self.assertRaises(SourceCraftError) as ctx:
            client.get_latest_scan("repo-1")

        self.assertNotIsInstance(ctx.exception, AppSecClientError)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_not_found_is_client_error(self):
        def handler(method, url, **kwargs):
            return _response(404, {"message": "missing"})

        client, _session = self._client(handler)

        with self.assertRaises(AppSecClientError) as ctx:
            client.list_findings("repo-1")

        self.assertEqual(ctx.exception.status_code, 404)
