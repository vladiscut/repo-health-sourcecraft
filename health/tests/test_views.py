from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from health.models import HealthScore, MetricSample, Repository, Scan, UserRepositoryAccess
from health.tests.helpers import grant_access, make_profile, make_repo
from health.views import PAGE_SIZE, _page_links
from integrations.sourcecraft import SourceCraftError


def _make_completed_scan(repo, *, docs=80, activity=70, ci_cd=None, security=None):
    scan = Scan.objects.create(
        repository=repo,
        status=Scan.Status.SUCCESS,
        triggered_by=Scan.TriggeredBy.SCHEDULE,
        finished_at=timezone.now(),
    )
    values = {
        MetricSample.Category.DOCS: docs,
        MetricSample.Category.ACTIVITY: activity,
        MetricSample.Category.CI_CD: ci_cd,
        MetricSample.Category.SECURITY: security,
        MetricSample.Category.ISSUES: 60,
        MetricSample.Category.CODE_HEALTH: 55,
    }
    for category, total in values.items():
        HealthScore.objects.create(
            scan=scan,
            category=category,
            total=total,
            weight_used=0.15,
            data_completeness=1.0 if total is not None else 0.0,
            raw_metrics={"reason": "публичный запуск"} if total is None else {},
        )
    return scan


class RepoListTests(TestCase):
    def setUp(self):
        make_repo(
            org_slug="divkit",
            repo_slug="divkit",
            language="C++",
            rating_value=100,
            likes=14,
        )
        make_repo(
            org_slug="acme",
            repo_slug="tools",
            language="Python",
            rating_value=10,
        )
        make_repo(
            org_slug="hidden",
            repo_slug="secret",
            language="Python",
            visibility=Repository.VisibilityType.PRIVATE,
            sourcecraft_id="private-1",
        )

    def test_public_list_hides_private_and_has_no_reset(self):
        response = self.client.get(reverse("health:repo-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "divkit/divkit")
        self.assertContains(response, "acme/tools")
        self.assertNotContains(response, "hidden/secret")
        self.assertNotContains(response, "Сбросить")

    def test_filter_by_language(self):
        response = self.client.get(reverse("health:repo-list"), {"lang": "Python"})
        self.assertContains(response, "acme/tools")
        self.assertNotContains(response, "divkit/divkit")
        self.assertContains(response, "Сбросить")
        self.assertContains(response, "по выбранным фильтрам")

    def test_search_query(self):
        response = self.client.get(reverse("health:repo-list"), {"q": "divkit"})
        self.assertContains(response, "divkit/divkit")
        self.assertNotContains(response, "acme/tools")

    def test_sort_by_rating(self):
        response = self.client.get(reverse("health:repo-list"), {"sort": "rating"})
        html = response.content.decode()
        self.assertLess(html.index("divkit/divkit"), html.index("acme/tools"))
        self.assertContains(response, "Сбросить")

    def test_reset_link_goes_home(self):
        response = self.client.get(
            reverse("health:repo-list"),
            {"q": "tools", "lang": "Python", "sort": "name"},
        )
        self.assertContains(
            response,
            f'href="{reverse("health:repo-list")}"',
        )

    def test_page_links_elide_distant_pages(self):
        page = Paginator(range(2556 * 50), 50).get_page(100)
        links = _page_links(page, "q=divkit&sort=rating")
        numbers = [link["number"] for link in links if "number" in link]
        self.assertEqual(numbers[0], 1)
        self.assertEqual(numbers[-1], 2556)
        self.assertIn(100, numbers)
        self.assertNotIn(50, numbers)
        self.assertTrue(any(link.get("ellipsis") for link in links))
        current = next(link for link in links if link.get("current"))
        self.assertEqual(current["href"], "?q=divkit&sort=rating&page=100")

    def test_pager_renders_numbers_and_jump(self):
        for index in range(PAGE_SIZE + 1):
            make_repo(repo_slug=f"paged-{index}", rating_value=index)
        response = self.client.get(reverse("health:repo-list"), {"sort": "name", "page": "2"})
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, "?sort=name&amp;page=1")
        self.assertContains(response, 'id="page-jump"')
        self.assertContains(response, "Перейти")


class RepoDetailAndExportTests(TestCase):
    def setUp(self):
        self.public = make_repo(org_slug="acme", repo_slug="tools")
        self.private = make_repo(
            org_slug="hidden",
            repo_slug="secret",
            visibility=Repository.VisibilityType.PRIVATE,
            sourcecraft_id="private-1",
        )

    def test_public_detail(self):
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Скачать Markdown")
        self.assertNotContains(response, "Скачать PDF")
        self.assertContains(response, "Запустить анализ")
        self.assertContains(
            response,
            f'<a href="{reverse("health:repo-list")}">← К списку</a>',
        )

    def test_detail_shows_null_categories_and_hides_score_while_scanning(self):
        _make_completed_scan(self.public)
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertFalse(response.context["scan_in_progress"])
        self.assertIsNotNone(response.context["score"])
        self.assertContains(response, "Нет данных")
        self.assertContains(response, "перераспределяется")
        self.assertContains(response, "публичный запуск")

    def test_detail_shows_what_score_is_made_of(self):
        scan = _make_completed_scan(self.public, docs=98)
        docs = scan.scores.get(category=MetricSample.Category.DOCS)
        docs.raw_metrics = {
            "readme_present": True,
            "readme_size_chars": 3200,
            "license_present": True,
            "license_type_recognized": True,
            "submetric_scores": {"readme_quality": 96, "license": 100},
        }
        docs.save(update_fields=["raw_metrics"])
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertContains(response, "README")
        self.assertContains(response, "вес 60%")
        self.assertContains(response, "3200 символов")
        self.assertContains(response, "Лицензия")
        self.assertContains(response, "тип распознан")

        Scan.objects.create(
            repository=self.public,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.USER,
        )
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertTrue(response.context["scan_in_progress"])
        self.assertIsNone(response.context["score"])
        self.assertContains(response, "Идёт анализ")
        self.assertContains(response, "data-scan-status-url")
        self.assertNotContains(response, "Скачать Markdown")
        self.assertNotContains(response, "Запустить анализ")

    def test_scan_status_pending_and_ready(self):
        url = reverse("health:repo-scan-status", args=["acme", "tools"])
        ready = self.client.get(url)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")

        active = Scan.objects.create(
            repository=self.public,
            status=Scan.Status.PENDING,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
        )
        pending = self.client.get(url)
        self.assertEqual(pending.json()["status"], "pending")
        self.assertEqual(pending.json()["scan_id"], active.id)

        active.status = Scan.Status.SUCCESS
        active.finished_at = timezone.now()
        active.save(update_fields=["status", "finished_at"])
        after = self.client.get(url)
        self.assertEqual(after.json()["status"], "ready")
        self.assertEqual(after.json()["scan_id"], active.id)

    def test_back_link_returns_to_personal_list(self):
        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"]),
            {"from": "me"},
        )
        self.assertContains(
            response,
            f'<a href="{reverse("health:my-repos")}">← К списку</a>',
        )
        self.assertContains(response, 'name="from" value="me"')

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_rescan_keeps_return_to_personal_list(self, apply_async):
        detail = reverse("health:repo-detail", args=["acme", "tools"])
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"]),
            {"from": "me"},
        )
        self.assertRedirects(response, f"{detail}?from=me")
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.public.id,
                "user_id": None,
                "force": True,
            },
            queue="analysis.scheduled",
        )
        self.assertTrue(
            Scan.objects.filter(
                repository=self.public,
                status=Scan.Status.PENDING,
            ).exists()
        )

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_owner_rescan_uses_user_queue(self, apply_async):
        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)

        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"])
        )

        self.assertEqual(response.status_code, 302)
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.public.id,
                "user_id": user.id,
                "force": True,
            },
            queue="analysis.user",
        )
        pending = Scan.objects.get(
            repository=self.public,
            status=Scan.Status.PENDING,
        )
        self.assertEqual(pending.triggered_by, Scan.TriggeredBy.USER)
        self.assertEqual(pending.triggered_by_user_id, user.id)

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_public_stranger_rescan_uses_scheduled_queue(self, apply_async):
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"])
        )
        self.assertEqual(response.status_code, 302)
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.public.id,
                "user_id": None,
                "force": True,
            },
            queue="analysis.scheduled",
        )
        self.assertTrue(
            Scan.objects.filter(
                repository=self.public,
                status=Scan.Status.PENDING,
            ).exists()
        )

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_rescan_hides_old_score_immediately(self, apply_async):
        _make_completed_scan(self.public)
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"]),
            follow=True,
        )
        self.assertTrue(response.context["scan_in_progress"])
        self.assertIsNone(response.context["score"])
        self.assertContains(response, "Идёт анализ")
        self.assertContains(response, "выгрузка недоступны")
        self.assertNotContains(response, "Запустить анализ")
        self.assertNotContains(response, "Скачать Markdown")
        apply_async.assert_called_once()

    def test_export_blocked_while_scan_in_progress(self):
        Scan.objects.create(
            repository=self.public,
            status=Scan.Status.PENDING,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
        )
        detail = reverse("health:repo-detail", args=["acme", "tools"])
        response = self.client.get(
            reverse("health:repo-export", args=["acme", "tools", "md"]),
            follow=True,
        )
        self.assertRedirects(response, detail)
        self.assertContains(response, "Выгрузка недоступна")

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_rescan_while_active_does_not_queue_again(self, apply_async):
        Scan.objects.create(
            repository=self.public,
            status=Scan.Status.RUNNING,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
        )
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"]),
            follow=True,
        )
        apply_async.assert_not_called()
        self.assertContains(response, "уже идёт")

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_scan_query_queues_scheduled_force_scan(self, apply_async):
        url = reverse("health:repo-detail", args=["acme", "tools"])
        response = self.client.get(url, {"scan": "true"})

        self.assertRedirects(response, url)
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.public.id,
                "user_id": None,
                "force": True,
            },
            queue="analysis.scheduled",
        )

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_scan_query_ignored_without_flag(self, apply_async):
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertEqual(response.status_code, 200)
        apply_async.assert_not_called()

    @patch("health.views.task_check_and_scan_repository.apply_async")
    def test_scan_query_on_private_without_access_is_404(self, apply_async):
        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"]),
            {"scan": "true"},
        )
        self.assertEqual(response.status_code, 404)
        apply_async.assert_not_called()

    @patch("health.views.task_check_and_scan_repository.apply_async")
    @patch("health.user_repository.SourceCraftClient")
    def test_scan_query_on_private_with_access_queues_scan(
        self, client_cls, apply_async
    ):
        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="pat-still-valid")
        self.client.force_login(user)
        client_cls.return_value.get_repository.return_value = {"id": "private-1"}
        url = reverse("health:repo-detail", args=["hidden", "secret"])

        response = self.client.get(url, {"scan": "true"})

        self.assertRedirects(response, url)
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.private.id,
                "user_id": None,
                "force": True,
            },
            queue="analysis.scheduled",
        )

    def test_private_detail_is_404(self):
        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )
        self.assertEqual(response.status_code, 404)

    @patch("health.user_repository.SourceCraftClient")
    def test_owner_can_open_private_detail_when_api_confirms(self, client_cls):
        user = get_user_model().objects.create_user("owner", password="x")
        profile = make_profile(user, sourcecraft_pat="pat-still-valid")
        self.assertTrue(profile.sourcecraft_token)
        self.client.force_login(user)
        client_cls.return_value.get_repository.return_value = {"id": "private-1"}

        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )

        self.assertEqual(response.status_code, 200)
        client_cls.assert_called_once_with(token="pat-still-valid")
        client_cls.return_value.get_repository.assert_called_once_with("private-1")

    @patch("health.user_repository.SourceCraftClient")
    def test_stale_access_row_is_not_enough_when_api_denies(self, client_cls):
        user = get_user_model().objects.create_user("former", password="x")
        make_profile(user, sourcecraft_pat="pat-revoked-rights")
        grant_access(user, self.private)
        self.client.force_login(user)
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "forbidden",
            status_code=403,
        )

        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.private,
            ).exists()
        )

    @patch("health.user_repository.SourceCraftClient")
    def test_private_detail_without_token_does_not_call_api(self, client_cls):
        user = get_user_model().objects.create_user("notoken", password="x")
        make_profile(user)
        grant_access(user, self.private)
        self.client.force_login(user)

        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )

        self.assertEqual(response.status_code, 404)
        client_cls.assert_not_called()
        self.assertTrue(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.private,
            ).exists()
        )

    @patch("health.user_repository.SourceCraftClient")
    def test_api_outage_hides_private_repo_but_keeps_access_row(self, client_cls):
        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="pat-still-valid")
        grant_access(user, self.private)
        self.client.force_login(user)
        client_cls.return_value.get_repository.side_effect = SourceCraftError(
            "unavailable",
            status_code=503,
        )

        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.private,
            ).exists()
        )

    def test_export_markdown_contains_score(self):
        _make_completed_scan(self.public)
        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        self.assertEqual(md.status_code, 200)
        body = md.content.decode("utf-8")
        self.assertIn("Repo Health", body)
        self.assertIn("acme/tools", body)
        self.assertIn("Документация", body)
        self.assertIn("Нет данных", body)
        self.assertIn("text/markdown", md["Content-Type"])
        self.assertIn("acme-tools-health.md", md["Content-Disposition"])

        pdf = self.client.get(reverse("health:repo-export", args=["acme", "tools", "pdf"]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.content, b"")
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_export_markdown_without_scan_is_not_empty(self):
        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        self.assertEqual(md.status_code, 200)
        self.assertIn("Нет данных", md.content.decode("utf-8"))
    def test_unknown_export_format_is_404(self):
        response = self.client.get(
            reverse("health:repo-export", args=["acme", "tools", "exe"])
        )
        self.assertEqual(response.status_code, 404)

    def test_private_export_is_404(self):
        response = self.client.get(
            reverse("health:repo-export", args=["hidden", "secret", "md"])
        )
        self.assertEqual(response.status_code, 404)


class RepoApiTests(TestCase):
    def setUp(self):
        make_repo(language="Python", repo_slug="py", rating_value=5)
        make_repo(language="Go", repo_slug="go", rating_value=9)
        make_repo(
            language="Python",
            repo_slug="secret",
            visibility=Repository.VisibilityType.PRIVATE,
            sourcecraft_id="private-api",
        )

    def test_api_hides_private_and_filters_language(self):
        response = self.client.get("/api/v1/repos/", {"language": "Python"})
        self.assertEqual(response.status_code, 200)
        slugs = [row["repo_slug"] for row in response.json()["results"]]
        self.assertEqual(slugs, ["py"])
