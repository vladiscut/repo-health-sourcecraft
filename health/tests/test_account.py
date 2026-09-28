from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from health.tests.helpers import grant_access, make_profile, make_repo


class AuthFlowTests(TestCase):
    def test_me_requires_login(self):
        response = self.client.get(reverse("health:my-repos"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/yandex/", response["Location"])

    def test_callback_rejects_bad_state(self):
        session = self.client.session
        session["yandex_oauth_state"] = "expected"
        session["yandex_oauth_verifier"] = "verifier"
        session.save()
        response = self.client.get(
            reverse("health:yandex-callback"),
            {"code": "abc", "state": "other"},
        )
        self.assertRedirects(response, reverse("health:repo-list"))

    @override_settings(YANDEX_CLIENT_ID="", YANDEX_CLIENT_SECRET="")
    def test_login_without_oauth_config(self):
        response = self.client.get(reverse("health:yandex-login"))
        self.assertRedirects(response, reverse("health:repo-list"))

    def test_logout_requires_post(self):
        user = get_user_model().objects.create_user("ivan", password="x")
        self.client.force_login(user)
        response = self.client.get(reverse("health:logout"))
        self.assertEqual(response.status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)


class MyReposTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ivan", password="x")
        make_profile(self.user)
        self.repo = make_repo(org_slug="ivan", repo_slug="app")
        grant_access(self.user, self.repo)
        self.client.force_login(self.user)

    def test_lists_accessible_repos(self):
        make_repo(org_slug="other", repo_slug="hidden", sourcecraft_id="other-1")
        response = self.client.get(reverse("health:my-repos"))
        self.assertContains(response, "ivan/app")
        self.assertContains(
            response,
            f'{reverse("health:repo-detail", args=["ivan", "app"])}?from=me',
        )
        self.assertNotContains(response, "other/hidden")
        self.assertContains(response, "Токен не задан")
        self.assertNotContains(response, "Отозвать токен")

    def test_connected_pat_is_visible(self):
        self.user.profile.sourcecraft_pat = "sc-pat-secret-token"
        self.user.profile.save()
        response = self.client.get(reverse("health:my-repos"))
        self.assertContains(response, "Токен подключён")
        self.assertContains(response, "•••• oken")
        self.assertContains(response, "Отозвать токен")
        self.assertNotContains(response, "sc-pat-secret-token")

    def test_revoke_pat_clears_token_and_access(self):
        self.user.profile.sourcecraft_pat = "sc-pat-secret-token"
        self.user.profile.save()
        response = self.client.post(reverse("health:revoke-pat"))
        self.assertRedirects(response, reverse("health:my-repos"))
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.sourcecraft_pat)
        self.assertFalse(self.user.repository_access.exists())
