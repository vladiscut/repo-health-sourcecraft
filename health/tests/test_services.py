from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from health.services import prepare_repository_data, sourcecraft_token_works, upsert_yandex_user


DIVKIT_PAYLOAD = {
    "id": "divkit-id",
    "slug": "divkit",
    "description": "UI framework",
    "web_url": "https://sourcecraft.dev/divkit/divkit",
    "visibility": "public",
    "is_empty": False,
    "organization": {"slug": "divkit"},
    "language": {"name": "C++"},
    "logo": {"url": "https://example.com/logo.png"},
    "counters": {"forks": "12"},
    "rating": {
        "value": 4061.5,
        "percentile": 1,
        "reaction_counts": [
            {"type": "positive_low", "count": "14"},
            {"type": "positive_medium", "count": "3"},
            {"type": "positive_high", "count": "7"},
        ],
    },
}


class PrepareRepositoryDataTests(SimpleTestCase):
    def test_maps_rating_and_three_reactions(self):
        data = prepare_repository_data(DIVKIT_PAYLOAD)
        self.assertIsNotNone(data)
        self.assertEqual(data["org_slug"], "divkit")
        self.assertEqual(data["repo_slug"], "divkit")
        self.assertEqual(data["language"], "C++")
        self.assertEqual(data["rating_value"], 4061.5)
        self.assertEqual(data["rating_percentile"], 1.0)
        self.assertEqual(data["likes"], 14)
        self.assertEqual(data["hearts"], 3)
        self.assertEqual(data["diamonds"], 7)
        self.assertEqual(data["forks"], 12)

    def test_missing_required_fields_returns_none(self):
        self.assertIsNone(prepare_repository_data({"slug": "only-slug"}))

    def test_language_as_string(self):
        payload = dict(DIVKIT_PAYLOAD, language="Go")
        data = prepare_repository_data(payload)
        self.assertEqual(data["language"], "Go")

    def test_missing_reactions_are_zero(self):
        payload = dict(DIVKIT_PAYLOAD, rating={"value": "10"})
        data = prepare_repository_data(payload)
        self.assertEqual(data["likes"], 0)
        self.assertEqual(data["hearts"], 0)
        self.assertEqual(data["diamonds"], 0)
        self.assertEqual(data["rating_value"], 10.0)


class SourcecraftTokenTests(SimpleTestCase):
    def test_empty_token_does_not_call_api(self):
        with patch("health.services.SourceCraftClient") as client_cls:
            ok, username = sourcecraft_token_works("")
        self.assertFalse(ok)
        self.assertEqual(username, "")
        client_cls.assert_not_called()


class UpsertYandexUserTests(TestCase):
    def test_creates_user_and_profile(self):
        user = upsert_yandex_user(
            {
                "id": "42",
                "login": "ivan",
                "default_email": "ivan@yandex.ru",
                "first_name": "Иван",
                "last_name": "Иванов",
            },
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        )
        self.assertEqual(user.username, "ivan")
        self.assertEqual(user.email, "ivan@yandex.ru")
        self.assertEqual(user.profile.ya_id, "42")
        self.assertEqual(user.profile.access_token, "at")

    def test_updates_existing_by_ya_id(self):
        first = upsert_yandex_user(
            {"id": "42", "login": "ivan", "default_email": "old@yandex.ru"},
            {"access_token": "old"},
        )
        second = upsert_yandex_user(
            {"id": "42", "login": "ivan", "default_email": "new@yandex.ru"},
            {"access_token": "new"},
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.email, "new@yandex.ru")
        self.assertEqual(second.profile.access_token, "new")
        self.assertEqual(get_user_model().objects.filter(username="ivan").count(), 1)
