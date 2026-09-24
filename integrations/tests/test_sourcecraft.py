"""Unit-тесты для :mod:`integrations.sourcecraft`."""

from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from integrations.sourcecraft import (
    DEFAULT_PAGE_SIZE,
    RETRY_STATUS_CODES,
    SourceCraftAPI,
    SourceCraftClient,
    SourceCraftError,
    SourceCraftFileClient,
    _build_retry,
)


class _NoLimit:
    """Заглушка лимитера: acquire() ничего не делает."""

    def acquire(self) -> None:
        pass


SETTINGS = override_settings(
    SOURCECRAFT_API_BASE_URL="https://api.example.test/",
    SOURCECRAFT_API_FILE_BASE_URL="https://files.example.test",
    SOURCECRAFT_API_TOKEN="token-from-settings",
)


def _response(status: int, body=None, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status
    response.content = b"x" if status not in (204,) else b""
    response.text = text
    if body is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = body
    return response


@SETTINGS
class SourceCraftErrorTests(SimpleTestCase):
    def test_defaults(self):
        exc = SourceCraftError("boom")
        self.assertEqual(str(exc), "boom")
        self.assertIsNone(exc.status_code)
        self.assertIsNone(exc.payload)

    def test_status_and_payload_stored(self):
        exc = SourceCraftError("boom", status_code=404, payload={"message": "no"})
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(exc.payload, {"message": "no"})
        self.assertIn("boom", str(exc))
        self.assertIn("no", str(exc))

    def test_string_payload_appended(self):
        exc = SourceCraftError("boom", payload="raw body")
        self.assertIn("raw body", str(exc))


@SETTINGS
class SourceCraftAPITests(SimpleTestCase):
    def _client(self, cls=SourceCraftClient, **kwargs):
        session = Mock()
        client = cls(session=session, rate_limiter=_NoLimit(), **kwargs)
        return client, session

    # -- __init__ -------------------------------------------------------

    def test_missing_base_url_setting_raises(self):
        class NoUrl(SourceCraftAPI):
            BASE_URL_SETTING = ""
            RATE_LIMIT_KEY_PREFIX = ""

        with self.assertRaises(NotImplementedError):
            NoUrl(rate_limiter=_NoLimit())

    def test_explicit_token_wins_over_settings(self):
        client, _ = self._client(token="explicit")
        self.assertEqual(client.access_token, "explicit")

    def test_none_token_uses_settings(self):
        client, _ = self._client(token=None)
        self.assertEqual(client.access_token, "token-from-settings")

    def test_base_url_rstrips_trailing_slash(self):
        client, _ = self._client()
        self.assertEqual(client.base_url, "https://api.example.test")

    def test_owns_session_false_for_provided(self):
        client, _ = self._client()
        self.assertFalse(client._owns_session)

    def test_max_pages_is_stored(self):
        client, _ = self._client(max_pages=3)
        self.assertEqual(client.max_pages, 3)

    # -- headers --------------------------------------------------------

    def test_headers_with_token(self):
        client, _ = self._client(token="abc")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Bearer abc")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIn("SourceCraftClient", headers["User-Agent"])

    def test_headers_without_token(self):
        client, _ = self._client(token=None)
        client.access_token = ""
        self.assertNotIn("Authorization", client._headers())

    # -- _request -------------------------------------------------------

    def test_request_builds_url_and_params(self):
        client, session = self._client()
        session.request.return_value = _response(200, {"ok": True})

        result = client._request("GET", "/user", params={"a": 1, "b": None})

        self.assertEqual(result, {"ok": True})
        args, kwargs = session.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "https://api.example.test/user")
        self.assertEqual(kwargs["params"], {"a": 1})
        self.assertEqual(kwargs["timeout"], client.timeout)

    def test_request_empty_params_are_none(self):
        client, session = self._client()
        session.request.return_value = _response(200, {})
        client._request("GET", "/user", params={"a": None})
        self.assertIsNone(session.request.call_args.kwargs["params"])

    def test_request_exception_wrapped(self):
        client, session = self._client()
        session.request.side_effect = requests.RequestException("nope")
        with self.assertRaises(SourceCraftError) as ctx:
            client._request("GET", "/user")
        self.assertIsNone(ctx.exception.status_code)

    def test_request_status_error_payload_from_json(self):
        client, session = self._client()
        session.request.return_value = _response(400, {"message": "bad"})
        with self.assertRaises(SourceCraftError) as ctx:
            client._request("GET", "/user")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.payload, {"message": "bad"})

    def test_request_status_error_payload_from_text(self):
        client, session = self._client()
        session.request.return_value = _response(500, None, text="x" * 600)
        with self.assertRaises(SourceCraftError) as ctx:
            client._request("GET", "/user")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.payload, "x" * 500)

    def test_request_204_returns_none(self):
        client, session = self._client()
        session.request.return_value = _response(204)
        self.assertIsNone(client._request("DELETE", "/user"))

    def test_request_empty_content_returns_none(self):
        client, session = self._client()
        response = _response(200, {})
        response.content = b""
        session.request.return_value = response
        self.assertIsNone(client._request("GET", "/user"))

    def test_request_bad_json_raises(self):
        client, session = self._client()
        session.request.return_value = _response(200, None)
        with self.assertRaises(SourceCraftError):
            client._request("GET", "/user")

    # -- pagination -----------------------------------------------------

    def test_fetch_page_adds_page_size_and_token(self):
        client, _ = self._client()
        with patch.object(
            SourceCraftClient,
            "_request",
            return_value={"items": [{"id": 1}], "next_page_token": "t2"},
        ) as mocked:
            items, token = client._fetch_page(
                "/repos", collection_key="items", page_size=5, page_token="t1"
            )
        self.assertEqual(items, [{"id": 1}])
        self.assertEqual(token, "t2")
        params = mocked.call_args.kwargs["params"]
        self.assertEqual(params["page_size"], 5)
        self.assertEqual(params["page_token"], "t1")

    def test_fetch_page_empty_response(self):
        client, _ = self._client()
        with patch.object(SourceCraftClient, "_request", return_value=None):
            items, token = client._fetch_page("/repos", collection_key="items")
        self.assertEqual(items, [])
        self.assertIsNone(token)

    def test_iter_pages_stops_without_next_token(self):
        client, _ = self._client()
        with patch.object(
            SourceCraftClient, "_fetch_page", return_value=([{"id": 1}], None)
        ):
            pages = list(client._iter_pages("/repos", collection_key="items"))
        self.assertEqual(len(pages), 1)

    def test_iter_pages_stops_on_repeated_token(self):
        """Повторный next_page_token — цикл.

        Дублирующая страница отдаётся один раз, дальше итерация
        прекращается, а не крутится бесконечно.
        """
        client, _ = self._client()
        with patch.object(
            SourceCraftClient, "_fetch_page", return_value=([{"id": 1}], "loop")
        ):
            pages = list(client._iter_pages("/repos", collection_key="items"))
        self.assertEqual(len(pages), 2)

    def test_iter_pages_respects_max_pages(self):
        client, _ = self._client(max_pages=2)
        tokens = iter(["t1", "t2", "t3", None])

        def fake_fetch(*args, **kwargs):
            return [{"id": 1}], next(tokens)

        with patch.object(SourceCraftClient, "_fetch_page", side_effect=fake_fetch):
            pages = list(client._iter_pages("/repos", collection_key="items"))
        self.assertEqual(len(pages), 2)

    def test_paginate_flattens_items(self):
        client, _ = self._client()
        with patch.object(
            SourceCraftClient,
            "_iter_pages",
            return_value=iter([([{"id": 1}], "t1"), ([{"id": 2}], None)]),
        ):
            items = list(client._paginate("/repos", collection_key="items"))
        self.assertEqual(items, [{"id": 1}, {"id": 2}])

    # -- lifecycle ------------------------------------------------------

    def test_close_does_not_close_borrowed_session(self):
        client, session = self._client()
        client.close()
        session.close.assert_not_called()

    def test_context_manager_returns_self(self):
        client, _ = self._client()
        with client as entered:
            self.assertIs(entered, client)

    # -- retry ----------------------------------------------------------

    def test_build_retry_covers_status_codes(self):
        retry = _build_retry()
        for code in RETRY_STATUS_CODES:
            self.assertIn(code, retry.status_forcelist)


@SETTINGS
class SourceCraftClientTests(SimpleTestCase):
    def _client(self) -> SourceCraftClient:
        return SourceCraftClient(
            token="token", session=Mock(), rate_limiter=_NoLimit()
        )

    def test_get_repository_path(self):
        client = self._client()
        with patch.object(
            SourceCraftClient, "_request", return_value={"id": "r1"}
        ) as mocked:
            self.assertEqual(client.get_repository("r1"), {"id": "r1"})
        self.assertEqual(mocked.call_args.args, ("GET", "/repos/id:r1"))

    def test_get_repository_file_tree_params(self):
        client = self._client()
        with patch.object(
            SourceCraftClient, "_paginate", return_value=iter([{"path": "a"}])
        ) as mocked:
            result = client.get_repository_file_tree("r1", since="abc")
        self.assertEqual(result, [{"path": "a"}])
        self.assertEqual(
            mocked.call_args.kwargs["params"],
            {"revision": "abc", "recursive": 1},
        )
        self.assertEqual(mocked.call_args.args[0], "/repos/id:r1/trees")

    def test_get_repository_file_tree_default_revision(self):
        client = self._client()
        with patch.object(
            SourceCraftClient, "_paginate", return_value=iter([])
        ) as mocked:
            client.get_repository_file_tree("r1")
        self.assertEqual(
            mocked.call_args.kwargs["params"],
            {"revision": "HEAD", "recursive": 1},
        )

    def test_get_issues_path_and_state(self):
        client = self._client()
        with patch.object(
            SourceCraftClient, "_paginate", return_value=iter([])
        ) as mocked:
            client.get_issues("r1", state="open")
        self.assertEqual(mocked.call_args.args[0], "/repos/id:r1/issues")
        self.assertEqual(mocked.call_args.kwargs["params"], {"state": "open"})

    def test_get_default_branch_hash_none_without_branches(self):
        client = self._client()
        with patch.object(SourceCraftClient, "_request", return_value={"branches": []}):
            self.assertIsNone(client.get_default_branch_hash("r1", "main"))

    def test_get_default_branch_hash_returns_hash(self):
        client = self._client()
        payload = {"branches": [{"commit": {"hash": "deadbeef"}}]}
        with patch.object(SourceCraftClient, "_request", return_value=payload):
            self.assertEqual(
                client.get_default_branch_hash("r1", "main"), "deadbeef"
            )

    def test_list_public_repositories_aggregates(self):
        client = self._client()
        with patch.object(
            SourceCraftClient,
            "_paginate",
            return_value=iter([{"id": 1}, {"id": 2}]),
        ):
            self.assertEqual(
                client.list_public_repositories(), [{"id": 1}, {"id": 2}]
            )

    def test_list_accessible_repositories_deduplicates(self):
        client = self._client()
        with patch.object(
            SourceCraftClient, "get_my_profile", return_value={"username": "me"}
        ), patch.object(
            SourceCraftClient,
            "get_billing_organization",
            return_value={"slug": "bill"},
        ), patch.object(
            SourceCraftClient,
            "list_organization_repositories",
            side_effect=[[{"id": "a"}, {"id": "b"}], [{"id": "b"}, {"id": "c"}]],
        ):
            result = client.list_accessible_repositories()
        self.assertEqual([item["id"] for item in result], ["a", "b", "c"])


@SETTINGS
class SourceCraftFileClientTests(SimpleTestCase):
    def _client(self):
        session = Mock()
        client = SourceCraftFileClient(
            token="token", session=session, rate_limiter=_NoLimit()
        )
        return client, session

    def test_accept_header_is_text(self):
        client, _ = self._client()
        self.assertEqual(client._headers()["Accept"], "text/plain, */*")

    def test_get_file_text_returns_body(self):
        client, session = self._client()
        response = Mock()
        response.status_code = 200
        response.text = "hello"
        session.request.return_value = response

        result = client.get_file_text("org", "repo", "/README.md", "abc")

        self.assertEqual(result, "hello")
        url = session.request.call_args.args[1]
        self.assertTrue(
            url.startswith("https://files.example.test/raw/org/repo/abc/")
        )

    def test_get_file_text_requires_path_and_revision(self):
        client, _ = self._client()
        with self.assertRaises(SourceCraftError):
            client.get_file_text("org", "repo", "/", "abc")
        with self.assertRaises(SourceCraftError):
            client.get_file_text("org", "repo", "/README.md", "")

    def test_get_file_text_requires_slugs(self):
        client, _ = self._client()
        with self.assertRaises(SourceCraftError):
            client.get_file_text("", "repo", "/README.md", "abc")
        with self.assertRaises(SourceCraftError):
            client.get_file_text("org", "", "/README.md", "abc")

    def test_get_file_text_404_raises_with_status(self):
        client, session = self._client()
        response = Mock()
        response.status_code = 404
        response.text = "missing"
        session.request.return_value = response

        with self.assertRaises(SourceCraftError) as ctx:
            client.get_file_text("org", "repo", "/README.md", "abc")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_file_text_request_exception_wrapped(self):
        client, session = self._client()
        session.request.side_effect = requests.RequestException("boom")
        with self.assertRaises(SourceCraftError) as ctx:
            client.get_file_text("org", "repo", "/README.md", "abc")
        self.assertIsNone(ctx.exception.status_code)
