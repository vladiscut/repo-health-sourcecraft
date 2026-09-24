from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.test import TestCase
from django.urls import reverse

from health.models import Repository, UserRepositoryAccess
from health.tests.helpers import grant_access, make_profile, make_repo
from health.views import PAGE_SIZE, _page_links
from integrations.sourcecraft import SourceCraftError


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
        self.assertContains(response, "Скачать PDF")

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

    def test_export_markdown_and_pdf_are_empty_files(self):
        md = self.client.get(reverse("health:repo-export", args=["acme", "tools", "md"]))
        self.assertEqual(md.status_code, 200)
        self.assertEqual(md.content, b"")
        self.assertIn("text/markdown", md["Content-Type"])
        self.assertIn("acme-tools-health.md", md["Content-Disposition"])

        pdf = self.client.get(reverse("health:repo-export", args=["acme", "tools", "pdf"]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.content, b"")
        self.assertEqual(pdf["Content-Type"], "application/pdf")

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
