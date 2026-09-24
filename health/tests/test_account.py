from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from health.models import Scan
from health.tests.helpers import grant_access, make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


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

    @patch("health.account.task_scan_user_repository.delay")
    def test_analyze_creates_pending_scan(self, delay):
        response = self.client.post(
            reverse("health:analyze-my-repo", args=["ivan", "app"]),
            {"next": reverse("health:my-repos")},
        )
        self.assertRedirects(response, reverse("health:my-repos"))
        scan = Scan.objects.get()
        self.assertEqual(scan.status, Scan.Status.PENDING)
        self.assertEqual(scan.triggered_by, Scan.TriggeredBy.USER)
        delay.assert_called_once_with(self.repo.id)

    @patch("health.account.task_scan_user_repository.delay")
    def test_analyze_rejects_open_redirect(self, delay):
        response = self.client.post(
            reverse("health:analyze-my-repo", args=["ivan", "app"]),
            {"next": "https://evil.example/phish"},
        )
        self.assertRedirects(
            response,
            reverse("health:repo-detail", args=["ivan", "app"]),
        )
        delay.assert_called_once()

    def test_analyze_foreign_repo_is_404(self):
        make_repo(org_slug="other", repo_slug="nope", sourcecraft_id="nope-1")
        response = self.client.post(
            reverse("health:analyze-my-repo", args=["other", "nope"]),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Scan.objects.exists())

    @patch("health.account.task_scan_user_repository.delay")
    @patch("health.user_repository.SourceCraftClient")
    def test_analyze_private_repo_requires_live_api_access(self, client_cls, delay):
        private = make_repo(
            org_slug="hidden",
            repo_slug="secret",
            visibility="private",
            sourcecraft_id="private-1",
        )
        grant_access(self.user, private)
        self.user.profile.sourcecraft_pat = "pat-revoked-rights"
        self.user.profile.save()
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "forbidden",
            status_code=403,
        )

        response = self.client.post(
            reverse("health:analyze-my-repo", args=["hidden", "secret"]),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Scan.objects.exists())
        delay.assert_not_called()
