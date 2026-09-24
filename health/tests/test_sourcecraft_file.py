from unittest.mock import Mock

from django.test import SimpleTestCase

from integrations.sourcecraft import SourceCraftFileClient, SourceCraftError


class _NoLimit:
    def acquire(self) -> None:
        return None


def _response(status: int, body: object, *, text: str | None = None) -> Mock:
    response = Mock()
    response.status_code = status
    response.content = b"{}"
    response.json.return_value = body
    response.text = text if text is not None else ""
    return response


class GetFileTextTests(SimpleTestCase):
    def _client(self, responses: list[Mock]) -> tuple[SourceCraftFileClient, Mock]:
        session = Mock()
        session.request.side_effect = responses
        client = SourceCraftFileClient(
            token="token",
            session=session,
            rate_limiter=_NoLimit(),
        )
        return client, session

    def test_200_returns_file_text(self):
        client, session = self._client([
            _response(
                200,
                {"slug": "repo", "organization": {"slug": "org"}},
            ),
            _response(200, None, text="hello\n"),
        ])

        text = client.get_file_text("org-1", "repo-1", "README.md", "abc123")

        self.assertEqual(text, "hello\n")
        file_call = session.request.call_args_list[1]
        self.assertEqual(file_call.args[0], "GET")
        self.assertEqual(
            file_call.args[1],
            "https://raw.sourcecraft.tech/raw/org/repo/abc123/README.md",
        )

    def test_404_raises_and_is_not_empty_text(self):
        client, _session = self._client([
            _response(
                200,
                {"slug": "repo", "organization": {"slug": "org"}},
            ),
            _response(404, {"message": "not found"}, text="not found"),
        ])

        with self.assertRaises(SourceCraftError) as ctx:
            client.get_file_text("org-1", "repo-1", "missing.md", "abc123")

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertNotEqual(str(ctx.exception), "")
