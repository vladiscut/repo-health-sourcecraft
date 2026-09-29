from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from health.models import Finding, HealthScore, MetricSample, Repository, Scan, UserRepositoryAccess
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

    def test_score_sort_puts_rows_without_score_last(self):
        high = make_repo(
            org_slug="sort",
            repo_slug="high",
            health_score=1,
            rating_value=1,
        )
        low = make_repo(
            org_slug="sort",
            repo_slug="low",
            health_score=99,
            rating_value=1,
        )
        ghost = make_repo(
            org_slug="sort",
            repo_slug="ghost",
            health_score=98,
            rating_value=5,
        )
        blank = make_repo(
            org_slug="sort",
            repo_slug="blank",
            health_score=70,
            rating_value=40,
        )
        _make_completed_scan(high, docs=90, activity=80, ci_cd=None, security=None)
        _make_completed_scan(low, docs=30, activity=20, ci_cd=None, security=None)
        blank_scan = Scan.objects.create(
            repository=blank,
            status=Scan.Status.SUCCESS,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
        )
        for category in MetricSample.Category:
            HealthScore.objects.create(
                scan=blank_scan,
                category=category,
                total=None,
                weight_used=0.0,
                data_completeness=0.0,
            )

        response = self.client.get(reverse("health:repo-list"))
        slugs = [item["repo"].repo_slug for item in response.context["items"]]
        self.assertEqual(
            slugs,
            ["high", "low", "divkit", "blank", "tools", "ghost"],
        )
        totals = []
        for item in response.context["items"]:
            score = item["score"]
            totals.append(
                None if score is None or score["total"] is None else score["total"]
            )
        self.assertEqual(totals, [70, 42, None, None, None, None])
        html = response.content.decode()
        self.assertLess(html.index("sort/high"), html.index("sort/low"))
        self.assertLess(html.index("sort/low"), html.index("sort/ghost"))
        self.assertContains(response, "нет данных")
        names = [
            name
            for item in response.context["items"]
            if item["score"]
            for name, _value in item["score"]["categories"]
        ]
        self.assertIn("Документация", names)
        self.assertNotIn("CI/CD", names)
        self.assertNotIn("Безопасность", names)
        self.assertContains(response, "PAT владельца")

    def test_preliminary_hides_number_on_detail(self):
        thin = make_repo(org_slug="thin", repo_slug="card", rating_value=2)
        scan = _make_completed_scan(
            thin, docs=None, activity=99, ci_cd=None, security=None
        )
        scan.scores.exclude(
            category=MetricSample.Category.ACTIVITY,
        ).update(total=None)
        scan.raw = {"health_score": 99, "is_preliminary": True}
        scan.save(update_fields=["raw"])

        response = self.client.get(
            reverse("health:repo-detail", args=["thin", "card"])
        )
        self.assertTrue(response.context["is_preliminary"])
        self.assertIsNone(response.context["score"]["total"])
        self.assertContains(response, "Оценка недоступна")
        self.assertContains(response, "API SourceCraft")
        self.assertNotContains(response, 'class="score big')

    def test_failed_scan_shows_previous_success(self):
        repo = make_repo(org_slug="fail", repo_slug="case", rating_value=2)
        success = _make_completed_scan(repo, docs=80, activity=70)
        success.raw = {"health_score": 66, "is_preliminary": False}
        success.finished_at = timezone.now()
        success.save(update_fields=["raw", "finished_at"])
        Scan.objects.create(
            repository=repo,
            status=Scan.Status.FAILED,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
            error="Scan помечен как зависший: не завершался более 1:00:00",
        )

        response = self.client.get(
            reverse("health:repo-detail", args=["fail", "case"])
        )
        self.assertEqual(response.context["scan"].id, success.id)
        self.assertEqual(response.context["score"]["total"], 66)
        self.assertContains(response, "предыдущего успешного скана")
        self.assertContains(response, "зависший")

    def test_sort_by_rating(self):
        response = self.client.get(reverse("health:repo-list"), {"sort": "rating"})
        html = response.content.decode()
        self.assertLess(html.index("divkit/divkit"), html.index("acme/tools"))
        self.assertContains(response, "Сбросить")
        self.assertContains(response, "PAT владельца")

    def test_public_list_omits_owner_only_categories(self):
        repo = make_repo(org_slug="shown", repo_slug="lib", rating_value=3)
        _make_completed_scan(repo)
        response = self.client.get(
            reverse("health:repo-list"),
            {"sort": "name", "q": "lib"},
        )
        item = response.context["items"][0]
        names = [name for name, _value in item["score"]["categories"]]
        self.assertIn("Документация", names)
        self.assertNotIn("CI/CD", names)
        self.assertNotIn("Безопасность", names)
        self.assertNotContains(response, "Безопасность нет")

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

    def test_scanned_repo_without_scores_shows_no_data(self):
        repo = make_repo(org_slug="empty", repo_slug="nobranch", default_branch="")
        scan = Scan.objects.create(
            repository=repo,
            status=Scan.Status.SUCCESS,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
        )
        for category, _label in MetricSample.Category.choices:
            HealthScore.objects.create(
                scan=scan,
                category=category,
                total=None,
                weight_used=0.0,
                data_completeness=0.0,
            )
        response = self.client.get(reverse("health:repo-list"), {"q": "nobranch"})
        row = next(item for item in response.context["items"] if item["repo"].pk == repo.pk)
        self.assertIsNone(row["score"]["total"])
        self.assertContains(response, "нет данных")
        self.assertNotContains(response, "None")

    def test_unscanned_repo_score_is_no_data(self):
        response = self.client.get(reverse("health:repo-list"), {"q": "divkit"})
        row = next(item for item in response.context["items"] if item["repo"].repo_slug == "divkit")
        self.assertIsNone(row["score"])
        self.assertContains(response, "нет данных")
        self.assertNotContains(response, "None")

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
        self.assertNotContains(response, "Запустить анализ")
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
        labels = [row["label"] for row in response.context["category_rows"]]
        self.assertNotIn("CI/CD", labels)
        self.assertNotIn("Безопасность", labels)
        self.assertIn("Документация", labels)
        self.assertContains(response, "PAT владельца")
        self.assertNotContains(response, "публичный запуск")
        self.assertNotContains(response, "перераспределяется")

        stranger = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"]),
            {"from": "me"},
        )
        stranger_labels = [row["label"] for row in stranger.context["category_rows"]]
        self.assertNotIn("CI/CD", stranger_labels)

        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
        owned = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"]),
            {"from": "me"},
        )
        owned_labels = [row["label"] for row in owned.context["category_rows"]]
        self.assertIn("CI/CD", owned_labels)
        self.assertIn("Безопасность", owned_labels)
        self.assertContains(owned, "публичный запуск")
        self.assertNotContains(owned, "PAT владельца")

    def test_detail_shows_no_data_when_scan_measured_nothing(self):
        scan = Scan.objects.create(
            repository=self.public,
            status=Scan.Status.SUCCESS,
            triggered_by=Scan.TriggeredBy.SCHEDULE,
            finished_at=timezone.now(),
        )
        for category, _label in MetricSample.Category.choices:
            HealthScore.objects.create(
                scan=scan,
                category=category,
                total=None,
                weight_used=0.0,
                data_completeness=0.0,
            )
        response = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertIsNone(response.context["score"]["total"])
        self.assertContains(response, "Нет данных")
        self.assertContains(response, "измерить метрики нельзя")
        self.assertContains(response, "нет данных")
        self.assertNotContains(response, "Score пока нет")
        self.assertNotContains(response, "None")

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
        self.assertContains(response, '<details class="bar-cat">')
        self.assertNotContains(response, "bar-cat\" open")
        self.assertContains(response, '<span class="bar-head">')
        self.assertNotContains(response, '<summary class="bar-head">')

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
        self.assertContains(response, "Анализируем")
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

        user = get_user_model().objects.create_user("owner", password="x")
        access = grant_access(user, self.public)
        access.status = UserRepositoryAccess.Status.CHECKING
        access.save(update_fields=["status"])
        checking = self.client.get(url)
        self.assertEqual(checking.json()["status"], "checking")
        self.assertIsNone(checking.json()["scan_id"])

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
        self.assertContains(response, "Запустить анализ")

    @patch("health.personal_scan.task_confirm_access_and_scan.apply_async")
    def test_rescan_keeps_return_to_personal_list(self, apply_async):
        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
        detail = reverse("health:repo-detail", args=["acme", "tools"])
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"]),
            {"from": "me"},
        )
        self.assertRedirects(response, f"{detail}?from=me")
        apply_async.assert_called_once_with(
            kwargs={
                "repository_id": self.public.id,
                "user_id": user.id,
            },
            queue="analysis.user",
        )
        self.assertFalse(Scan.objects.filter(repository=self.public).exists())
        self.assertTrue(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.public,
                status=UserRepositoryAccess.Status.CHECKING,
            ).exists()
        )

    @patch("health.personal_scan.task_confirm_access_and_scan.apply_async")
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
            },
            queue="analysis.user",
        )
        self.assertFalse(Scan.objects.filter(repository=self.public).exists())
        access = UserRepositoryAccess.objects.get(user=user, repository=self.public)
        self.assertEqual(access.status, UserRepositoryAccess.Status.CHECKING)

    @patch("health.personal_scan.task_confirm_access_and_scan.apply_async")
    def test_public_stranger_cannot_rescan(self, apply_async):
        stranger = get_user_model().objects.create_user("stranger", password="x")
        self.client.force_login(stranger)
        detail = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"])
        )
        self.assertNotContains(detail, "Запустить анализ")

        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"])
        )
        self.assertEqual(response.status_code, 404)
        apply_async.assert_not_called()
        self.assertFalse(
            UserRepositoryAccess.objects.filter(
                repository=self.public,
                status=UserRepositoryAccess.Status.CHECKING,
            ).exists()
        )

    @patch("health.personal_scan.task_confirm_access_and_scan.apply_async")
    def test_rescan_hides_old_score_immediately(self, apply_async):
        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
        _make_completed_scan(self.public)
        response = self.client.post(
            reverse("health:repo-rescan", args=["acme", "tools"]),
            follow=True,
        )
        self.assertTrue(response.context["scan_in_progress"])
        self.assertIsNone(response.context["score"])
        self.assertContains(response, "Проверяем доступ к репозиторию")
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

    @patch("health.personal_scan.task_confirm_access_and_scan.apply_async")
    def test_rescan_while_active_does_not_queue_again(self, apply_async):
        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
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
    @patch("health.access_check.SourceCraftClient")
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

    @patch("health.access_check.SourceCraftClient")
    def test_running_access_check_opens_card_without_api(self, client_cls):
        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="pat-still-valid")
        access = grant_access(user, self.private)
        access.status = UserRepositoryAccess.Status.CHECKING
        access.save(update_fields=["status"])
        self.client.force_login(user)

        response = self.client.get(
            reverse("health:repo-detail", args=["hidden", "secret"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Проверяем доступ к репозиторию")
        client_cls.assert_not_called()

    @patch("health.access_check.SourceCraftClient")
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
        self.assertEqual(client_cls.call_args.kwargs["token"], "pat-still-valid")
        self.assertEqual(client_cls.call_args.kwargs["timeout"], 5.0)
        client_cls.return_value.get_repository.assert_called_once_with("private-1")

    @patch("health.access_check.SourceCraftClient")
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
            reverse("health:repo-detail", args=["hidden", "secret"]),
            follow=True,
        )

        self.assertContains(response, "Нет доступа к репозиторию.")
        self.assertFalse(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.private,
            ).exists()
        )

    @patch("health.access_check.SourceCraftClient")
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

    @patch("health.access_check.SourceCraftClient")
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

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Не удалось проверить доступ")
        self.assertTrue(
            UserRepositoryAccess.objects.filter(
                user=user,
                repository=self.private,
            ).exists()
        )

    def test_http_evidence_is_a_link_on_the_card_and_in_markdown(self):
        scan = _make_completed_scan(self.public)
        page_url = "https://sourcecraft.dev/acme/tools/cicd/runs/run-1"
        Finding.objects.create(
            scan=scan,
            category=MetricSample.Category.CI_CD,
            severity=Finding.Severity.LOW,
            title="Последний прогон CI успешный",
            detail="Последний завершённый прогон завершился успешно.",
            recommendation="",
            evidence_refs=[page_url, ".sourcecraft/ci.yaml"],
            estimated_score_impact=0,
        )

        public = self.client.get(reverse("health:repo-detail", args=["acme", "tools"]))
        self.assertNotContains(public, "Последний прогон CI")
        self.assertNotContains(public, page_url)

        user = get_user_model().objects.create_user("owner", password="x")
        grant_access(user, self.public)
        self.client.force_login(user)
        page = self.client.get(
            reverse("health:repo-detail", args=["acme", "tools"]),
            {"from": "me"},
        )
        self.assertContains(page, f'href="{page_url}"')
        self.assertNotContains(page, 'href=".sourcecraft/ci.yaml"')

        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        body = md.content.decode("utf-8")
        self.assertIn(page_url, body)
        self.assertNotIn(".sourcecraft/ci.yaml", body)
        self.assertNotIn("к Score", page.content.decode("utf-8"))

    def test_finding_shows_score_impact(self):
        # Балл категории 90, сырой вклад 25 не влезает в запас 10.
        # Вес категории 0.20 → прирост общего Score равен 2.
        scan = _make_completed_scan(self.public)
        HealthScore.objects.filter(
            scan=scan,
            category=MetricSample.Category.CODE_HEALTH,
        ).update(total=90, weight_used=0.20)
        Finding.objects.create(
            scan=scan,
            category=MetricSample.Category.CODE_HEALTH,
            severity=Finding.Severity.HIGH,
            title="Не найдено тестов",
            detail="Тестов нет.",
            recommendation="Добавьте тесты.",
            estimated_score_impact=25,
        )

        page = self.client.get(reverse("health:repo-detail", args=["acme", "tools"]))
        self.assertContains(page, "+2 к Score")
        self.assertNotContains(page, "+25 к Score")

        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        body = md.content.decode("utf-8")
        self.assertIn("Ожидаемый прирост Repo Health Score: +2", body)
        self.assertNotIn("+25", body)

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
